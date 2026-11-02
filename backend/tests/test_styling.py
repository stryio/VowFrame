"""Looks (V5.0): candidates the user generates and picks, one edit each from the uploaded photo, and the looks
every sample, photo set, slot action and face swap is shot from. Since V7.0 the bride has looks per style, with
that style's makeup, and the groom's looks serve every style."""

import json

from app.db import Database
from app.generator import GeneratorError
from app.prompts import STYLING_VERSION
from app.storage import asset_path
from conftest import (PNG, BlockingGenerator, ScriptedGenerator, create_director_photo_batch, create_project, ensure_looks,
                      preview_batch, uploaded_ids, wait_batch, wait_until)
from test_face_swap import ScriptedFaceSwapper


def detail(client, project_id):
    return client.get(f'/api/projects/{project_id}').json()


def preview_slot(client, project_id, style_id='korean_minimal'):
    return next(slot for slot in detail(client, project_id)['previews'] if slot['style_id'] == style_id)


def make_looks(client, project_id, person, items, *, style_id=None, request_id=None, select=True):
    """Generate candidates (a bride's for style_id), wait for them and (by default) select them in order, so the
    last one is the one used; returns their ids."""
    body = {'client_request_id': request_id or f'{person}-{style_id}-{items}', 'person': person, 'items': items}
    if style_id is not None:
        body['style_id'] = style_id
    response = client.post(f'/api/projects/{project_id}/looks', json=body)
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


def test_look_options_list_hairstyles_and_the_style_table(client_app):
    client, _app = client_app
    options = client.get('/api/look-options').json()
    assert [item['id'] for item in options['hairstyles'] if item['role'] == 'bride'] == [
        'keep', 'low_bun', 'high_bun', 'french_bun', 'chinese_bun', 'finger_waves', 'half_up', 'big_waves', 'soft_waves',
        'low_ponytail', 'side_braid', 'short_inward', 'short_curls', 'slick_short']
    assert [item['id'] for item in options['hairstyles'] if item['role'] == 'groom'] == [
        'keep', 'side_part', 'textured', 'side_back', 'korean_part']
    assert all('default_color' not in item for item in options['hairstyles'])
    assert {item['id'] for item in options['hairstyles'] if item['updo']} == {
        'low_bun', 'high_bun', 'french_bun', 'chinese_bun', 'finger_waves', 'slick_short'}
    assert len(options['makeup_colors']) == 8 and 'max_selected' not in options
    assert options['styles']['hanfu'] == {'primary': 'soft_pink', 'alternates': ['classic_red', 'rose_bean'], 'needs_updo': True}
    assert options['styles']['korean_minimal'] == {'primary': 'milk_tea', 'alternates': ['soft_pink'], 'needs_updo': False}


def test_six_bride_and_four_groom_candidates_by_default_fitted_to_the_hair_length(client_app):
    client, _app = client_app
    project = create_project(client)
    items = detail(client, project['id'])['default_look_items']
    assert items['bride'] == [{'hairstyle_id': hairstyle}
                              for hairstyle in ('keep', 'low_bun', 'soft_waves', 'half_up', 'high_bun', 'french_bun')]
    assert [item['hairstyle_id'] for item in items['groom']] == ['keep', 'side_part', 'textured', 'side_back']
    client.patch(f"/api/projects/{project['id']}", json={'bride_hair_length': 'short'})
    items = detail(client, project['id'])['default_look_items']['bride']
    # Short hair: the short styles replace the long ones, then her own hairstyle, once.
    assert [item['hairstyle_id'] for item in items] == ['keep', 'slick_short', 'short_inward', 'short_curls']


def test_candidates_are_one_edit_each_of_the_uploaded_photo_with_the_project_settings(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'refined', 'groom_hair_length': 'buzz'})
        assert [item['hairstyle_id'] for item in detail(client, project['id'])['default_look_items']['groom']] == ['keep']
        url = f"/api/projects/{project['id']}/looks"
        bad = (({'person': 'b', 'items': [{'hairstyle_id': 'side_part'}]}, 'invalid_hairstyle'),  # a buzz cut
               ({'person': 'a', 'style_id': 'korean_minimal', 'items': [{'hairstyle_id': 'side_part'}]}, 'invalid_hairstyle'),
               ({'person': 'a', 'items': [{'hairstyle_id': 'keep'}]}, 'invalid_style'),          # a bride look needs a style
               ({'person': 'a', 'style_id': 'nowhere', 'items': [{'hairstyle_id': 'keep'}]}, 'invalid_style'),
               ({'person': 'a', 'style_id': 'black_white', 'items': [{'hairstyle_id': 'keep'}]}, 'invalid_style'),  # retired
               ({'person': 'b', 'style_id': 'korean_minimal', 'items': [{'hairstyle_id': 'keep'}]}, 'invalid_style'))
        for body, code in bad:
            response = client.post(url, json={'client_request_id': code + str(body), **body})
            assert response.status_code == 422 and response.json()['detail']['code'] == code, response.text
        # The makeup colour comes from the style, never from the request (V7.0).
        response = client.post(url, json={'client_request_id': 'colour', 'person': 'a', 'style_id': 'korean_minimal',
                                          'items': [{'hairstyle_id': 'keep', 'makeup_color': 'berry'}]})
        assert response.status_code == 422
        ids = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'high_bun'}],
                         style_id='korean_minimal', select=False)
        calls = app.state.stylist.calls
        # V7.5: each bride look is two edits, makeup on the uploaded photo, then only the hair on that made-up photo.
        assert len(calls) == 4 and all(len(call[0]) == 1 for call in calls)
        makeup_calls = [call for call in calls if '这一步只化妆、修整皮肤和换背景' in call[1]]
        hair_calls = [call for call in calls if '这一步只改头发' in call[1]]
        assert len(makeup_calls) == 2 and len(hair_calls) == 2
        assert all(call[0][0].stem == project['person_a']['asset_id'] for call in makeup_calls)
        makeup = makeup_calls[0][1]
        assert '新娘化妆色饱满、层次鲜明、细节精致的影楼婚礼新娘妆' in makeup and '新娘妆色：眼影用奶咖棕和米杏色' in makeup and '新娘妆面：底妆是清透柔光的质感' in makeup
        assert '发型：' not in makeup
        hair = next(call[1] for call in hair_calls if '发型：赫本高盘发，' in call[1])
        assert '不再上妆、不再提白' in hair and '新娘妆色' not in hair
        looks = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert [looks[look_id]['status'] for look_id in ids] == ['ready', 'ready']
        made_up = {looks[look_id]['makeup_asset_id'] for look_id in ids}
        assert None not in made_up and {call[0][0].stem for call in hair_calls} == made_up
        assert uploaded_ids(app, list(made_up)) == [project['person_a']['asset_id']] * 2
        assert {looks[look_id]['style_id'] for look_id in ids} == {'korean_minimal'}
        assert looks[ids[1]]['makeup_level'] == 'refined' and looks[ids[1]]['makeup_color'] == 'milk_tea'
        # Without scores the first one ready is picked for the style; the other waits.
        assert sorted(looks[look_id]['selected'] for look_id in ids) == [False, True]
        assert {looks[look_id]['selected_by'] for look_id in ids} == {None, 'auto'}
        assert all(looks[look_id]['usable'] for look_id in ids)
        assert uploaded_ids(app, [looks[ids[0]]['asset_id']]) == [project['person_a']['asset_id']]
        groom = make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}], select=False)[0]
        look = next(look for look in detail(client, project['id'])['looks'] if look['id'] == groom)
        assert look['style_id'] is None and look['makeup_color'] is None and look['selected']
        assert '皮肤和精修：' in calls[-1][1] and '新娘妆色' not in calls[-1][1]


def test_a_repeated_request_creates_the_candidates_once(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        body = {'client_request_id': 'twice', 'person': 'b', 'items': [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'textured'}]}
        first = client.post(f"/api/projects/{project['id']}/looks", json=body)
        again = client.post(f"/api/projects/{project['id']}/looks", json=body)
        assert (first.status_code, again.status_code) == (201, 200)
        assert [look['id'] for look in first.json()] == [look['id'] for look in again.json()]
        other = client.post(f"/api/projects/{project['id']}/looks", json={**body, 'person': 'a', 'items': [{'hairstyle_id': 'keep'}]})
        assert other.status_code == 409 and other.json()['detail']['code'] == 'request_id_reused'
        wait_until(lambda: detail(client, project['id'])['looks'], lambda looks: all(look['status'] == 'ready' for look in looks))
        assert len(app.state.stylist.calls) == 2


def test_each_style_uses_one_selected_bride_look_and_every_style_the_same_groom_look(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        first, second = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'soft_waves'}],
                                   style_id='korean_minimal')
        bun = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'low_bun'}], style_id='hanfu')[0]
        grooms = make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'textured'}])
        looks = detail(client, project['id'])['style_looks']
        # Selecting a look replaces the one its style used; other styles keep theirs.
        assert looks['korean_minimal'] == {'bride': second, 'groom': grooms[1]}
        assert looks['hanfu'] == {'bride': bun, 'groom': grooms[1]}
        assert looks['french_vintage'] == {'bride': None, 'groom': grooms[1]}
        state = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert [state[look_id]['selected'] for look_id in (first, second, bun)] == [False, True, True]
        assert client.post(f'/api/looks/{first}/select').status_code == 200
        assert detail(client, project['id'])['style_looks']['korean_minimal']['bride'] == first
        assert client.post(f'/api/looks/{first}/unselect').status_code == 200
        assert detail(client, project['id'])['style_looks']['korean_minimal']['bride'] is None
        assert client.delete(f'/api/looks/{bun}').status_code == 204
        remaining = detail(client, project['id'])
        assert bun not in [look['id'] for look in remaining['looks']] and remaining['style_looks']['hanfu']['bride'] is None


def test_samples_use_the_styles_selected_looks(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        url = f"/api/projects/{project['id']}/previews"
        missing = client.post(url, json={'client_request_id': 'no-looks', 'style_ids': ['korean_minimal']})
        assert missing.status_code == 409 and missing.json()['detail']['code'] == 'looks_required'
        looks = ensure_looks(client, project['id'], ['korean_minimal'])['style_looks']['korean_minimal']
        # The groom's look serves every style, but the bride has none for this one yet.
        response = client.post(url, json={'client_request_id': 'no-bride', 'style_ids': ['korean_minimal', 'french_vintage']})
        assert response.status_code == 409 and '「城市街拍」还没有选用新娘的造型' in response.json()['detail']['message']
        response = client.post(url, json={'client_request_id': 'ok', 'style_ids': ['korean_minimal']})
        assert response.status_code == 201
        task = batch_tasks(client, response.json()['id'])[0]
        assert json.loads(task_row(app, task['id'])['model_config_snapshot'])['looks'] == {'version': STYLING_VERSION, **looks}


def test_the_chinese_styles_need_the_bride_in_an_updo(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        response = client.post(f"/api/projects/{project['id']}/looks", json={
            'client_request_id': 'own', 'person': 'a', 'style_id': 'hanfu', 'items': [{'hairstyle_id': 'keep'}]})
        assert response.status_code == 422 and response.json()['detail']['code'] == 'invalid_hairstyle'
        assert '需要盘发' in response.json()['detail']['message']
        batch = preview_batch(client, project['id'], request_id='hanfu-ok', style_ids=['hanfu'])
        wait_batch(client, batch['id'])
        prompt = batch_tasks(client, batch['id'])[0]['prompt']
        assert '头饰：在造型照的发型上，新娘戴一顶素雅的小花冠' in prompt.split('\n\n')[2]


def test_a_bride_with_short_hair_keeps_her_own_hair_in_the_chinese_styles(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        client.patch(f"/api/projects/{project['id']}", json={'bride_hair_length': 'short'})
        own = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep'}], style_id='hanfu')[0]
        assert detail(client, project['id'])['style_looks']['hanfu']['bride'] == own


def test_the_bride_look_wears_the_styles_makeup_and_the_shot_copies_it(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'refined'})
        batch = preview_batch(client, project['id'], request_id='makeup', style_ids=['chinese_traditional'])
        wait_batch(client, batch['id'])
        look = next(call[1] for call in app.state.stylist.calls if '新娘婚礼当天化好妆' in call[1])
        assert '新娘化妆色饱满、层次鲜明、细节精致的影楼婚礼新娘妆' in look and '新娘妆色：眼影用暖棕、赤茶和浅金色' in look
        paragraphs = batch_tasks(client, batch['id'])[0]['prompt'].split('\n\n')
        assert '造型照就是两人婚礼当天化好妆、做好发型后的样子' in paragraphs[1] and '新娘化妆色饱满、层次鲜明、细节精致的影楼婚礼新娘妆' not in paragraphs[1]
        assert '新娘妆色：眼影用暖棕、赤茶和浅金色，腮红用暖粉和杏红色，唇色是正红或朱砂红。' in paragraphs[2]


def test_looks_from_before_v7_make_no_new_pictures(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        old = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'low_bun'}], style_id='korean_minimal')[0]
        failed = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep'}], style_id='korean_minimal', select=False)[0]
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE looks SET styling_version=? WHERE id IN (?,?)', (STYLING_VERSION - 1, old, failed))
            conn.execute("UPDATE looks SET status='failed' WHERE id=?", (failed,))
        look = next(item for item in detail(client, project['id'])['looks'] if item['id'] == old)
        assert not look['usable'] and '旧版本的造型' in look['unusable_reason']
        assert detail(client, project['id'])['style_looks']['korean_minimal']['bride'] is None
        response = client.post(f'/api/looks/{failed}/retry')
        assert response.status_code == 409 and response.json()['detail']['code'] == 'look_unusable'


def test_photo_sets_and_slot_actions_keep_the_looks_of_their_sample(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        sample = preview_slot(client, project['id'])
        # A newer look for the style does not change pictures that start from the sample.
        make_looks(client, project['id'], 'a', [{'hairstyle_id': 'soft_waves'}], style_id='korean_minimal')
        batch = create_director_photo_batch(client, project['id'], sample['id'], request_id='set')
        wait_batch(client, batch.json()['id'])
        images = [look_image(app, sample['look_ids']['bride']), look_image(app, sample['look_ids']['groom'])]
        for task in batch_tasks(client, batch.json()['id']):
            row = task_row(app, task['id'])
            assert json.loads(row['model_config_snapshot'])['looks'] == {'version': STYLING_VERSION, **sample['look_ids']}
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
        looks = ensure_looks(client, project['id'], ['korean_minimal'])['style_looks']['korean_minimal']
        client.post(f"/api/projects/{project['id']}/images", data={'person': 'a'}, files={'file': ('new.png', PNG, 'image/png')})
        state = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert not state[looks['bride']]['usable'] and state[looks['bride']]['unusable_reason'].startswith('这个人的照片已经更换')
        assert state[looks['groom']]['usable']
        assert detail(client, project['id'])['style_looks']['korean_minimal'] == {'bride': None, 'groom': looks['groom']}
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
    # Two looks (three edits: the bride's is makeup then hair), then the groom's left side look is refused.
    stylist = ScriptedGenerator([None, None, None, GeneratorError('auth_or_quota')])
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
        side_edits = [call for call in stylist.calls[3:] if '脸朝画面' in call[1]]
        assert len(stylist.calls) == 6 and len(side_edits) == 3 and all(len(call[0]) == 2 for call in side_edits)


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
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_fix='facefusion', concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        sample = preview_slot(client, project['id'])
        with app.state.db.connect() as conn:
            paths = {row['id']: row['file_path'] for row in conn.execute('SELECT id,file_path FROM image_assets')}
        bride_image, groom_image = look_image(app, sample['look_ids']['bride']), look_image(app, sample['look_ids']['groom'])
        bride, groom = swapper.calls[0][0], swapper.calls[0][1]
        assert bride.front.as_posix().endswith(paths[bride_image]) and groom.front.as_posix().endswith(paths[groom_image])
        # A newer look for the style does not change a manual swap of the old picture.
        make_looks(client, project['id'], 'a', [{'hairstyle_id': 'soft_waves'}], style_id='korean_minimal')
        swap = client.post(f"/api/slots/{sample['id']}/face-swap", json={'client_request_id': 'manual'})
        wait_batch(client, swap.json()['id'])
        assert swapper.calls[-1][0].front.as_posix().endswith(paths[bride_image])


def test_candidates_show_their_likeness_to_the_uploaded_photo(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper) as (client, _app):
        project = create_project(client)
        make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep'}], style_id='korean_minimal', select=False)
        make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}], select=False)
        looks = wait_until(lambda: detail(client, project['id'])['looks'], lambda looks: all(look['face_score'] is not None for look in looks))
        # The scripted scorer gives the bride 0.55 and the groom 0.61; each look keeps its own person's score.
        assert {look['role']: look['face_score'] for look in looks} == {'bride': 0.55, 'groom': 0.61}
        # Each look is compared with the uploaded photo it was made from.
        with _app.state.db.connect() as conn:
            uploads = {row['id']: row['file_path'] for row in conn.execute('SELECT id,file_path FROM image_assets')}
        sources = {uploads[project['person_a']['asset_id']], uploads[project['person_b']['asset_id']]}
        # The bride's look is scored twice, its made-up step and the finished look (V7.5).
        assert len(swapper.scored) == 3 and all(item.bride == item.groom for item in swapper.scored)
        bride = next(look for look in looks if look['role'] == 'bride')
        assert bride['makeup_face_score'] == 0.55 and bride['selected_by'] == 'auto'
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
    assert {'person', 'role', 'style_id', 'angle', 'parent_look_id', 'hairstyle_id', 'makeup_color', 'selected_order',
            'face_score'} <= columns
    assert ('project_id', 'projects', 'CASCADE') in foreign_keys and ('parent_look_id', 'looks', 'NO ACTION') in foreign_keys


def test_the_most_similar_look_is_picked_unless_the_user_picked_one(app_factory):
    from app.styling import auto_select
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        first, second = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'low_bun'}],
                                   style_id='korean_minimal', select=False)

        def state():
            looks = {look['id']: look for look in detail(client, project['id'])['looks']}
            return {look_id: (looks[look_id]['selected'], looks[look_id]['selected_by']) for look_id in (first, second)}

        # Scores pick the most similar look of the style (V7.5), not the first one ready.
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE looks SET face_score=? WHERE id=?', (0.41, first))
            conn.execute('UPDATE looks SET face_score=? WHERE id=?', (0.63, second))
            row = conn.execute('SELECT * FROM looks WHERE id=?', (first,)).fetchone()
            auto_select(conn, conn.execute('SELECT * FROM projects WHERE id=?', (project['id'],)).fetchone(), row)
        assert state() == {first: (False, None), second: (True, 'auto')}
        # The user's pick stays, whatever the scores say.
        assert client.post(f'/api/looks/{first}/select').status_code == 200
        with app.state.db.connect(write=True) as conn:
            auto_select(conn, conn.execute('SELECT * FROM projects WHERE id=?', (project['id'],)).fetchone(), row)
        assert state() == {first: (True, 'user'), second: (False, None)}


def test_a_failed_hair_edit_is_retried_from_the_saved_made_up_photo(app_factory):
    stylist = ScriptedGenerator([None, GeneratorError('rejected')])
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, concurrency=1) as (client, _app):
        project = create_project(client)
        look_id = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'low_bun'}], style_id='korean_minimal', select=False)[0]
        look = next(look for look in detail(client, project['id'])['looks'] if look['id'] == look_id)
        assert look['status'] == 'failed' and look['makeup_asset_id']
        assert client.post(f'/api/looks/{look_id}/retry').status_code == 200
        wait_until(lambda: detail(client, project['id'])['looks'], lambda looks: looks[0]['status'] == 'ready')
        # Makeup, the refused hair edit, and the hair edit again: the makeup is not paid for twice.
        assert len(stylist.calls) == 3 and '这一步只改头发' in stylist.calls[2][1]
        assert stylist.calls[2][0][0].stem == look['makeup_asset_id']
