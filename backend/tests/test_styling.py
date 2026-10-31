"""Looks (V5.0): candidates the user generates and picks, one edit each from the uploaded photo, and the looks
every sample, photo set, slot action and face swap is shot from."""

import json

from app.db import Database
from app.generator import GeneratorError
from app.storage import asset_path
from conftest import (PNG, BlockingGenerator, ScriptedGenerator, create_director_photo_batch, create_project, ensure_looks, looks_for,
                      preview_batch, uploaded_ids, wait_batch, wait_until)
from test_face_swap import ScriptedFaceSwapper


def detail(client, project_id):
    return client.get(f'/api/projects/{project_id}').json()


def preview_slot(client, project_id, style_id='korean_minimal'):
    return next(slot for slot in detail(client, project_id)['previews'] if slot['style_id'] == style_id)


def make_looks(client, project_id, person, items, *, request_id=None, select=True):
    """Generate candidates, wait for them and (by default) select them in order; returns their ids."""
    response = client.post(f'/api/projects/{project_id}/looks',
                           json={'client_request_id': request_id or f'{person}-{items}', 'person': person, 'items': items})
    assert response.status_code == 201, response.text
    ids = [look['id'] for look in response.json()]
    wait_until(lambda: detail(client, project_id)['looks'],
               lambda looks: all(look['status'] not in ('waiting', 'running') for look in looks if look['id'] in ids))
    if select:
        for look_id in ids:
            assert client.post(f'/api/looks/{look_id}/select').status_code == 200
    return ids


def look_image(app, look_id):
    with app.state.db.connect() as conn:
        return conn.execute('SELECT image_asset_id FROM looks WHERE id=?', (look_id,)).fetchone()[0]


def task_row(app, task_id):
    with app.state.db.connect() as conn:
        return dict(conn.execute('SELECT * FROM generation_tasks WHERE id=?', (task_id,)).fetchone())


def batch_tasks(client, batch_id):
    return sorted(client.get('/api/tasks', params={'batch_id': batch_id}).json(), key=lambda task: task['created_at'])


def test_look_options_list_hairstyles_colours_and_the_style_table(client_app):
    client, _app = client_app
    options = client.get('/api/look-options').json()
    assert [(item['role'], item['id']) for item in options['hairstyles']] == [
        ('bride', 'keep'), ('bride', 'low_bun'), ('bride', 'soft_waves'), ('bride', 'side_braid'), ('bride', 'high_bun'),
        ('groom', 'keep'), ('groom', 'side_part'), ('groom', 'textured')]
    assert len(options['makeup_colors']) == 8 and options['max_selected'] == 3
    assert options['styles']['hanfu'] == {'primary': 'soft_pink', 'alternates': ['classic_red', 'rose_bean'], 'needs_updo': True}
    assert options['styles']['korean_minimal'] == {'primary': 'milk_tea', 'alternates': ['soft_pink'], 'needs_updo': False}


def test_candidates_are_one_edit_each_of_the_uploaded_photo_with_the_project_settings(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'refined', 'groom_hair_length': 'buzz'})
        assert [item['hairstyle_id'] for item in detail(client, project['id'])['default_look_items']['groom']] == ['keep']
        url = f"/api/projects/{project['id']}/looks"
        bad = (({'person': 'b', 'items': [{'hairstyle_id': 'side_part'}]}, 'invalid_hairstyle'),  # a buzz cut
               ({'person': 'b', 'items': [{'hairstyle_id': 'keep', 'makeup_color': 'berry'}]}, 'invalid_makeup_color'),
               ({'person': 'a', 'items': [{'hairstyle_id': 'low_bun'}]}, 'invalid_makeup_color'),
               ({'person': 'a', 'items': [{'hairstyle_id': 'side_part', 'makeup_color': 'berry'}]}, 'invalid_hairstyle'))
        for body, code in bad:
            response = client.post(url, json={'client_request_id': code + str(body), **body})
            assert response.status_code == 422 and response.json()['detail']['code'] == code
        ids = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep', 'makeup_color': 'milk_tea'},
                                                      {'hairstyle_id': 'high_bun', 'makeup_color': 'classic_red'}], select=False)
        calls = app.state.stylist.calls
        assert len(calls) == 2 and all(len(call[0]) == 1 for call in calls)
        assert all(call[0][0].stem == project['person_a']['asset_id'] for call in calls)
        assert any('新娘化精致的影楼新娘妆' in call[1] and '发型：饱满的高盘发' in call[1] and '唇色是正红或朱砂红' in call[1] for call in calls)
        looks = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert [looks[look_id]['status'] for look_id in ids] == ['ready', 'ready']
        assert looks[ids[1]]['makeup_level'] == 'refined' and looks[ids[1]]['makeup_color_name'] == '正红朱砂'
        assert not looks[ids[0]]['selected'] and looks[ids[0]]['usable']
        assert uploaded_ids(app, [looks[ids[0]]['asset_id']]) == [project['person_a']['asset_id']]


def test_a_repeated_request_creates_the_candidates_once(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        body = {'client_request_id': 'twice', 'person': 'b', 'items': [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'textured'}]}
        first = client.post(f"/api/projects/{project['id']}/looks", json=body)
        again = client.post(f"/api/projects/{project['id']}/looks", json=body)
        assert (first.status_code, again.status_code) == (201, 200)
        assert [look['id'] for look in first.json()] == [look['id'] for look in again.json()]
        other = client.post(f"/api/projects/{project['id']}/looks", json={**body, 'person': 'a', 'items': [{'hairstyle_id': 'keep', 'makeup_color': 'berry'}]})
        assert other.status_code == 409 and other.json()['detail']['code'] == 'request_id_reused'
        wait_until(lambda: detail(client, project['id'])['looks'], lambda looks: all(look['status'] == 'ready' for look in looks))
        assert len(app.state.stylist.calls) == 2


def test_selection_has_an_order_a_default_and_a_limit(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        items = [{'hairstyle_id': hairstyle, 'makeup_color': 'milk_tea'} for hairstyle in ('keep', 'low_bun', 'soft_waves', 'side_braid')]
        ids = make_looks(client, project['id'], 'a', items, select=False)
        for look_id in ids[:3]:
            assert client.post(f'/api/looks/{look_id}/select').status_code == 200
        too_many = client.post(f'/api/looks/{ids[3]}/select')
        assert too_many.status_code == 409 and too_many.json()['detail']['code'] == 'too_many_looks'
        assert client.post(f'/api/looks/{ids[2]}/make-default').status_code == 200
        selected = sorted((look for look in detail(client, project['id'])['looks'] if look['selected']), key=lambda look: look['selected_order'])
        assert [look['id'] for look in selected] == [ids[2], ids[0], ids[1]]
        assert client.post(f'/api/looks/{ids[3]}/make-default').status_code == 409  # not selected
        assert client.post(f'/api/looks/{ids[0]}/unselect').status_code == 200
        assert client.delete(f'/api/looks/{ids[1]}').status_code == 204
        remaining = detail(client, project['id'])['looks']
        assert ids[1] not in [look['id'] for look in remaining]
        assert [look['id'] for look in remaining if look['selected']] == [ids[2]]
        assert client.post(f'/api/looks/{ids[3]}/select').status_code == 200


def test_samples_need_selected_usable_looks_for_every_style(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        url = f"/api/projects/{project['id']}/previews"
        missing = client.post(url, json={'client_request_id': 'no-looks', 'style_ids': ['korean_minimal']})
        assert missing.status_code == 422 and missing.json()['detail']['code'] == 'invalid_looks'
        looks = looks_for(client, project['id'], ['korean_minimal'])
        unselected = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep', 'makeup_color': 'berry'}], select=False)[0]
        response = client.post(url, json={'client_request_id': 'unselected', 'style_ids': ['korean_minimal'],
                                          'looks': {'korean_minimal': {**looks['korean_minimal'], 'bride': unselected}}})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'look_unusable'
        swapped = {'bride': looks['korean_minimal']['groom'], 'groom': looks['korean_minimal']['bride']}
        response = client.post(url, json={'client_request_id': 'wrong-role', 'style_ids': ['korean_minimal'], 'looks': {'korean_minimal': swapped}})
        assert response.status_code == 422 and response.json()['detail']['code'] == 'invalid_look'
        assert client.post(url, json={'client_request_id': 'ok', 'style_ids': ['korean_minimal'], 'looks': looks}).status_code == 201


def test_the_chinese_styles_need_the_bride_in_an_updo(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        own = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep', 'makeup_color': 'milk_tea'}])[0]
        groom = make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}])[0]
        defaults = detail(client, project['id'])['default_looks']
        assert defaults['hanfu']['bride'] is None and defaults['korean_minimal']['bride'] == own
        response = client.post(f"/api/projects/{project['id']}/previews", json={
            'client_request_id': 'hanfu', 'style_ids': ['hanfu'], 'looks': {'hanfu': {'bride': own, 'groom': groom}}})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'look_unfit'
        bun = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'low_bun', 'makeup_color': 'classic_red'}])[0]
        defaults = detail(client, project['id'])['default_looks']
        assert defaults['hanfu']['bride'] == bun and defaults['korean_minimal']['bride'] == own
        batch = preview_batch(client, project['id'], request_id='hanfu-ok', style_ids=['hanfu'])
        wait_batch(client, batch['id'])
        prompt = batch_tasks(client, batch['id'])[0]['prompt']
        assert '头饰：在造型照的发型上，新娘戴一顶素雅的小花冠' in prompt.split('\n\n')[2]


def test_a_bride_with_short_hair_keeps_her_own_hair_in_the_chinese_styles(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        client.patch(f"/api/projects/{project['id']}", json={'bride_hair_length': 'short'})
        own = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep', 'makeup_color': 'classic_red'}])[0]
        assert detail(client, project['id'])['default_looks']['hanfu']['bride'] == own


def test_each_style_gets_the_first_look_in_its_recommended_colour(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        peach, red, milk = make_looks(client, project['id'], 'a', [
            {'hairstyle_id': 'keep', 'makeup_color': 'peach_coral'}, {'hairstyle_id': 'low_bun', 'makeup_color': 'classic_red'},
            {'hairstyle_id': 'soft_waves', 'makeup_color': 'milk_tea'}])
        first_groom, _second_groom = make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'textured'}])
        defaults = detail(client, project['id'])['default_looks']
        assert defaults['korean_minimal']['bride'] == milk          # recommended colour
        assert defaults['seaside_sunset']['bride'] == peach         # recommended colour
        assert defaults['chinese_traditional']['bride'] == red      # the only updo, in its recommended colour
        assert defaults['french_vintage']['bride'] == peach         # an alternate colour
        assert defaults['city_night']['bride'] == peach             # neither: the first selected look
        assert {value['groom'] for value in defaults.values()} == {first_groom}


def test_photo_sets_and_slot_actions_keep_the_looks_of_their_sample(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        sample = preview_slot(client, project['id'])
        # A newer default look does not change pictures that start from the sample.
        newer = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'soft_waves', 'makeup_color': 'milk_tea'}])[0]
        client.post(f'/api/looks/{newer}/make-default')
        batch = create_director_photo_batch(client, project['id'], sample['id'], request_id='set')
        wait_batch(client, batch.json()['id'])
        images = [look_image(app, sample['look_ids']['bride']), look_image(app, sample['look_ids']['groom'])]
        for task in batch_tasks(client, batch.json()['id']):
            row = task_row(app, task['id'])
            assert json.loads(row['model_config_snapshot'])['looks'] == {'version': 2, **sample['look_ids']}
            assert json.loads(row['reference_image_ids'])[:2] == images
        photo = next(photo for photo in detail(client, project['id'])['photos'] if photo['shot_size'] != 'detail')
        boost = client.post(f"/api/slots/{photo['id']}/regenerate", json={'client_request_id': 'boost', 'mode': 'identity_boost'})
        wait_batch(client, boost.json()['id'])
        assert json.loads(task_row(app, batch_tasks(client, boost.json()['id'])[0]['id'])['reference_image_ids'])[:2] == images


def test_pictures_from_the_old_flow_cannot_start_new_shots(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        task = batch_tasks(client, batch['id'])[0]
        with app.state.db.connect(write=True) as conn:
            snapshot = json.loads(task_row(app, task['id'])['model_config_snapshot'])
            snapshot.pop('looks')
            conn.execute('UPDATE generation_tasks SET model_config_snapshot=? WHERE id=?', (json.dumps(snapshot), task['id']))
        slot = preview_slot(client, project['id'])
        boost = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'boost', 'mode': 'identity_boost'})
        assert boost.status_code == 409 and boost.json()['detail']['code'] == 'looks_required'
        draft = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': slot['id'], 'count': 4}).json()
        photos = client.post(f"/api/projects/{project['id']}/photo-batches", json={'client_request_id': 'set', 'draft_id': draft['id']})
        assert photos.status_code == 409 and photos.json()['detail']['code'] == 'looks_required'


def test_looks_of_a_replaced_photo_or_after_swapping_roles_make_no_new_pictures(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        looks = looks_for(client, project['id'], ['korean_minimal'])['korean_minimal']
        client.post(f"/api/projects/{project['id']}/images", data={'person': 'a'}, files={'file': ('new.png', PNG, 'image/png')})
        state = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert not state[looks['bride']]['usable'] and state[looks['bride']]['unusable_reason'].startswith('这个人的照片已经更换')
        assert state[looks['groom']]['usable']
        assert detail(client, project['id'])['default_looks']['korean_minimal'] == {'bride': None, 'groom': looks['groom']}
        assert client.post(f"/api/looks/{looks['bride']}/select").status_code == 409
        client.patch(f"/api/projects/{project['id']}", json={'person_a_role': 'groom'})
        state = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert state[looks['groom']]['unusable_reason'].startswith('交换新娘和新郎之后')


def test_a_restart_interrupts_a_running_candidate_and_a_retry_edits_it_again(app_factory, tmp_path):
    data_dir = tmp_path / 'restart'
    with app_factory(generator=ScriptedGenerator(), data_dir=data_dir) as (client, app):
        project = create_project(client)
        look_id = make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}], select=False)[0]
        db = app.state.db
    # Put the candidate back where a restart would find it: still being edited.
    with db.connect(write=True) as conn:
        conn.execute("UPDATE looks SET status='running',image_asset_id=NULL WHERE id=?", (look_id,))
    with app_factory(generator=ScriptedGenerator(), data_dir=data_dir) as (client, app):
        look = next(look for look in detail(client, project['id'])['looks'] if look['id'] == look_id)
        assert (look['status'], look['error_code']) == ('interrupted', 'interrupted_by_restart')
        assert app.state.stylist.calls == []
        assert client.post(f'/api/looks/{look_id}/retry').status_code == 200
        wait_until(lambda: detail(client, project['id'])['looks'], lambda looks: looks[0]['status'] == 'ready')
        assert len(app.state.stylist.calls) == 1
        assert client.post(f'/api/looks/{look_id}/retry').status_code == 409  # only failed or interrupted ones


def test_queued_candidates_never_start_after_a_restart(app_factory, tmp_path):
    data_dir = tmp_path / 'queued'
    with app_factory(generator=ScriptedGenerator(), data_dir=data_dir) as (client, app):
        project = create_project(client)
        look_id = make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}], select=False)[0]
        db = app.state.db
    with db.connect(write=True) as conn:
        conn.execute("UPDATE looks SET status='waiting',image_asset_id=NULL WHERE id=?", (look_id,))
    with app_factory(generator=ScriptedGenerator(), data_dir=data_dir) as (client, app):
        look = next(look for look in detail(client, project['id'])['looks'] if look['id'] == look_id)
        assert (look['status'], look['error_code']) == ('failed', 'not_started')
        assert app.state.stylist.calls == []


def test_a_failed_candidate_is_retried_on_its_own(app_factory):
    stylist = ScriptedGenerator([None, GeneratorError('rejected')])
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, concurrency=1) as (client, _app):
        project = create_project(client)
        ids = make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'textured'}], select=False)
        state = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert [state[look_id]['status'] for look_id in ids] == ['ready', 'failed']
        assert state[ids[1]]['error_code'] == 'rejected'
        assert client.post(f'/api/looks/{ids[1]}/select').status_code == 409
        assert client.post(f'/api/looks/{ids[1]}/retry').status_code == 200
        wait_until(lambda: detail(client, project['id'])['looks'], lambda looks: all(look['status'] == 'ready' for look in looks))
        assert len(stylist.calls) == 3


def test_side_looks_are_made_once_from_the_side_photo_and_the_front_look(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=4) as (client, app):
        project = create_project(client)
        for person, angle in (('a', 'right'), ('b', 'left')):
            client.post(f"/api/projects/{project['id']}/images", data={'person': person, 'angle': angle},
                        files={'file': (f'{person}-{angle}.png', PNG, 'image/png')})
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        sample = preview_slot(client, project['id'])
        batch = create_director_photo_batch(client, project['id'], sample['id'], request_id='set')
        wait_batch(client, batch.json()['id'])
        side_edits = [call for call in app.state.stylist.calls if '脸朝画面' in call[1]]
        # Mock shots: the groom faces left in two shots, the bride right in one: two side looks, each edited once.
        assert len(side_edits) == 2
        groom_front = look_image(app, sample['look_ids']['groom'])
        with app.state.db.connect() as conn:
            groom_front_path = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (groom_front,)).fetchone()[0]
        groom_edit = next(call for call in side_edits if '新郎脸朝画面左侧' in call[1])
        assert groom_edit[0][1].as_posix().endswith(groom_front_path)
        with app.state.db.connect() as conn:
            sides = conn.execute("SELECT parent_look_id,angle FROM looks WHERE angle<>'front' ORDER BY angle").fetchall()
        assert [tuple(row) for row in sides] == [(sample['look_ids']['groom'], 'left'), (sample['look_ids']['bride'], 'right')]
        # Side looks never show up as candidates.
        assert len(detail(client, project['id'])['looks']) == 2


def test_a_failed_side_look_holds_the_set_and_a_retry_reuses_the_rest(app_factory):
    # Two looks, then the groom's left side look is refused for lack of balance.
    stylist = ScriptedGenerator([None, None, GeneratorError('auth_or_quota')])
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, concurrency=1) as (client, _app):
        project = create_project(client)
        for person, angle in (('a', 'right'), ('b', 'left')):
            client.post(f"/api/projects/{project['id']}/images", data={'person': person, 'angle': angle},
                        files={'file': (f'{person}-{angle}.png', PNG, 'image/png')})
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        batch = create_director_photo_batch(client, project['id'], preview_slot(client, project['id'])['id'], request_id='set')
        batch_id = batch.json()['id']
        wait_batch(client, batch_id, status='held')
        tasks = batch_tasks(client, batch_id)
        assert [task['status'] for task in tasks] == ['completed', 'failed', 'held', 'held']
        assert client.post(f"/api/tasks/{tasks[1]['id']}/retry").status_code == 201
        assert client.post(f'/api/batches/{batch_id}/continue').status_code == 200
        wait_batch(client, batch_id)
        side_edits = [call for call in stylist.calls[2:] if '脸朝画面' in call[1]]
        assert len(stylist.calls) == 5 and len(side_edits) == 3 and all(len(call[0]) == 2 for call in side_edits)


def test_a_side_look_whose_file_is_gone_is_edited_again(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        client.post(f"/api/projects/{project['id']}/images", data={'person': 'b', 'angle': 'left'},
                    files={'file': ('b-left.png', PNG, 'image/png')})
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        sample = preview_slot(client, project['id'])
        wait_batch(client, create_director_photo_batch(client, project['id'], sample['id'], request_id='first').json()['id'])
        with app.state.db.connect() as conn:
            stored = conn.execute("SELECT asset.file_path FROM looks JOIN image_assets asset ON asset.id=looks.image_asset_id "
                                  "WHERE looks.angle='left'").fetchone()[0]
        asset_path(app.state.config, stored).unlink()
        wait_batch(client, create_director_photo_batch(client, project['id'], sample['id'], request_id='second').json()['id'])
        assert sum('脸朝画面' in call[1] for call in app.state.stylist.calls) == 2
        with app.state.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM looks WHERE angle='left' AND discarded_at IS NOT NULL").fetchone()[0] == 1


def test_face_swaps_use_the_looks_of_the_picture_they_fix(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_swap=True, concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        sample = preview_slot(client, project['id'])
        with app.state.db.connect() as conn:
            paths = {row['id']: row['file_path'] for row in conn.execute('SELECT id,file_path FROM image_assets')}
        bride_image, groom_image = look_image(app, sample['look_ids']['bride']), look_image(app, sample['look_ids']['groom'])
        bride, groom = swapper.calls[0][0], swapper.calls[0][1]
        assert bride.front.as_posix().endswith(paths[bride_image]) and groom.front.as_posix().endswith(paths[groom_image])
        # A newer default look does not change a manual swap of the old picture.
        newer = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'soft_waves', 'makeup_color': 'milk_tea'}])[0]
        client.post(f'/api/looks/{newer}/make-default')
        swap = client.post(f"/api/slots/{sample['id']}/face-swap", json={'client_request_id': 'manual'})
        wait_batch(client, swap.json()['id'])
        assert swapper.calls[-1][0].front.as_posix().endswith(paths[bride_image])


def test_candidates_show_their_likeness_to_the_uploaded_photo(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper) as (client, _app):
        project = create_project(client)
        make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep', 'makeup_color': 'milk_tea'}], select=False)
        make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}], select=False)
        looks = wait_until(lambda: detail(client, project['id'])['looks'], lambda looks: all(look['face_score'] is not None for look in looks))
        # The scripted scorer gives the bride 0.55 and the groom 0.61; each look keeps its own person's score.
        assert {look['role']: look['face_score'] for look in looks} == {'bride': 0.55, 'groom': 0.61}
        # Each look is compared with the uploaded photo it was made from.
        with _app.state.db.connect() as conn:
            uploads = {row['id']: row['file_path'] for row in conn.execute('SELECT id,file_path FROM image_assets')}
        sources = {uploads[project['person_a']['asset_id']], uploads[project['person_b']['asset_id']]}
        assert len(swapper.scored) == 2 and all(item.bride == item.groom for item in swapper.scored)
        assert {next(path for path in sources if item.bride.as_posix().endswith(path)) for item in swapper.scored} == sources


def test_a_v4_task_without_its_styled_photos_is_retired(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        task = batch_tasks(client, batch['id'])[0]
        uploads = [project['person_a']['asset_id'], project['person_b']['asset_id']]
        # A V4.0 sample that failed before its styled photos were made, then retried.
        with app.state.db.connect(write=True) as conn:
            snapshot = json.loads(task_row(app, task['id'])['model_config_snapshot'])
            snapshot.pop('looks')
            snapshot['styling'] = {'version': 1, 'bride': {'makeup': 'natural', 'hair': 'long'}, 'groom': {'makeup': 'natural', 'hair': 'short'}}
            conn.execute("UPDATE generation_tasks SET status='failed',model_config_snapshot=?,reference_image_ids=? WHERE id=?",
                         (json.dumps(snapshot), json.dumps(uploads), task['id']))
        assert client.post(f"/api/tasks/{task['id']}/retry").status_code == 201
        wait_batch(client, batch['id'], status='needs_attention')
        retry = batch_tasks(client, batch['id'])[-1]
        assert retry['error_code'] == 'styling_retired' and len(app.state.generator.calls) == 1


def test_pictures_from_before_styled_photos_replay_and_swap_with_the_uploaded_photos(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        task = batch_tasks(client, batch['id'])[0]
        uploads = [project['person_a']['asset_id'], project['person_b']['asset_id']]
        with app.state.db.connect(write=True) as conn:
            snapshot = json.loads(task_row(app, task['id'])['model_config_snapshot'])
            snapshot.pop('looks'), snapshot.pop('reference_slots')
            conn.execute('UPDATE generation_tasks SET model_config_snapshot=?,reference_image_ids=?,prompt=? WHERE id=?',
                         (json.dumps(snapshot), json.dumps(uploads), 'legacy prompt', task['id']))
            paths = {row['id']: row['file_path'] for row in conn.execute('SELECT id,file_path FROM image_assets')}
        slot = preview_slot(client, project['id'])
        assert slot['styled_asset_ids'] is None and slot['look_ids'] is None
        stylist_calls = len(app.state.stylist.calls)
        same = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'same', 'mode': 'same'})
        wait_batch(client, same.json()['id'])
        replay = task_row(app, batch_tasks(client, same.json()['id'])[0]['id'])
        assert json.loads(replay['reference_image_ids']) == uploads and replay['prompt'] == 'legacy prompt'
        swap = client.post(f"/api/slots/{slot['id']}/face-swap", json={'client_request_id': 'swap'})
        assert swap.status_code == 201, swap.text
        wait_batch(client, swap.json()['id'])
        bride, groom = swapper.calls[-1][0], swapper.calls[-1][1]
        assert bride.front.as_posix().endswith(paths[uploads[0]]) and groom.front.as_posix().endswith(paths[uploads[1]])
        assert len(app.state.stylist.calls) == stylist_calls


def test_a_project_is_not_deleted_while_a_look_is_being_edited(app_factory):
    stylist = BlockingGenerator()
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, concurrency=1) as (client, _app):
        project = create_project(client)
        client.post(f"/api/projects/{project['id']}/looks",
                    json={'client_request_id': 'slow', 'person': 'b', 'items': [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'textured'}]})
        assert stylist.started.wait(5)
        response = client.delete(f"/api/projects/{project['id']}")
        assert response.status_code == 409 and response.json()['detail']['code'] == 'project_busy'
        statuses = sorted(look['status'] for look in detail(client, project['id'])['looks'])
        assert statuses == ['failed', 'running']  # the queued one is cancelled, never sent
        stylist.release.set()
        wait_until(lambda: detail(client, project['id'])['looks'], lambda looks: 'running' not in [look['status'] for look in looks])
        assert client.delete(f"/api/projects/{project['id']}").status_code == 204
        assert len(stylist.calls) == 1


def test_looks_go_with_their_project(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        ensure_looks(client, project['id'])
        assert client.delete(f"/api/projects/{project['id']}").status_code == 204
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM looks').fetchone()[0] == 0
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_an_old_database_gets_the_look_table(tmp_path):
    db = Database(tmp_path / 'old.db')
    db.initialize()
    with db.connect(write=True) as conn:
        conn.execute('DROP TABLE looks')
    db.initialize()
    with db.connect() as conn:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(looks)')}
        foreign_keys = {(row['from'], row['table'], row['on_delete']) for row in conn.execute('PRAGMA foreign_key_list(looks)')}
    assert {'person', 'role', 'angle', 'parent_look_id', 'hairstyle_id', 'makeup_color', 'selected_order', 'face_score'} <= columns
    assert ('project_id', 'projects', 'CASCADE') in foreign_keys and ('parent_look_id', 'looks', 'NO ACTION') in foreign_keys
