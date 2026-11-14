"""Makeup candidates (V7.8): the bride's makeup for one style is generated as candidates on her uploaded photo,
hair unchanged; one is selected per style (the most similar unless the user picks), and every new bride look of the
style is only a hair edit of that made-up photo."""

import pytest

from app.generator import GeneratorError
from conftest import ScriptedGenerator, create_project, wait_until
from test_face_swap import ScriptedFaceSwapper


def detail(client, project_id):
    return client.get(f'/api/projects/{project_id}').json()


def make_makeups(client, project_id, count, *, style_id='city_story', request_id=None):
    response = client.post(f'/api/projects/{project_id}/makeups', json={
        'client_request_id': request_id or f'makeup-{style_id}-{count}', 'person': 'a', 'style_id': style_id, 'count': count})
    assert response.status_code == 201, response.text
    ids = [item['id'] for item in response.json()]
    wait_until(lambda: detail(client, project_id)['makeups'],
               lambda makeups: all(item['status'] not in ('waiting', 'running') for item in makeups if item['id'] in ids))
    return ids


def makeup_state(client, project_id):
    return {item['id']: item for item in detail(client, project_id)['makeups']}


def make_bride_looks(client, project_id, hairstyles, *, style_id='city_story', request_id='hair'):
    response = client.post(f'/api/projects/{project_id}/looks', json={
        'client_request_id': request_id, 'person': 'a', 'style_id': style_id,
        'items': [{'hairstyle_id': hairstyle} for hairstyle in hairstyles]})
    assert response.status_code == 201, response.text
    ids = [look['id'] for look in response.json()]
    wait_until(lambda: detail(client, project_id)['looks'],
               lambda looks: all(look['status'] not in ('waiting', 'running') for look in looks if look['id'] in ids))
    return ids


def test_makeup_candidates_are_one_makeup_edit_each_of_the_uploaded_photo(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        url = f"/api/projects/{project['id']}/makeups"
        bad = (({'person': 'b', 'style_id': 'city_story', 'count': 1}, 422, 'invalid_style'),  # the groom's: no style
               ({'person': 'a', 'count': 1}, 422, 'invalid_style'),                                  # the bride's: one
               ({'person': 'a', 'style_id': 'nowhere', 'count': 1}, 422, 'invalid_style'),
               ({'person': 'a', 'style_id': 'black_white', 'count': 1}, 422, 'invalid_style'))     # retired
        for body, status, code in bad:
            response = client.post(url, json={'client_request_id': code + str(body), **body})
            assert response.status_code == status and response.json()['detail']['code'] == code, response.text
        assert client.post(url, json={'client_request_id': 'many', 'person': 'a', 'style_id': 'city_story', 'count': 5}).status_code == 422
        ids = make_makeups(client, project['id'], 3)
        calls = app.state.stylist.calls
        assert len(calls) == 3 and all(len(images) == 1 and images[0].stem == project['person_a']['asset_id'] for images, _p, _a in calls)
        assert all('这一步只化妆、修整皮肤和换背景' in prompt and '发型：' not in prompt for _i, prompt, _a in calls)
        assert '新娘妆色：眼影用玫瑰棕和藕粉色' in calls[0][1]
        state = makeup_state(client, project['id'])
        assert [state[makeup_id]['status'] for makeup_id in ids] == ['ready'] * 3
        assert {state[makeup_id]['makeup_color'] for makeup_id in ids} == {'rose_bean'}
        # Without scores the first one ready is selected for the style.
        assert sorted((state[makeup_id]['selected'], state[makeup_id]['selected_by']) for makeup_id in ids) == [
            (False, None), (False, None), (True, 'auto')]
        # A repeated request creates them once.
        again = client.post(url, json={'client_request_id': 'makeup-city_story-3', 'person': 'a', 'style_id': 'city_story', 'count': 3})
        assert again.status_code == 200 and [item['id'] for item in again.json()] == ids and len(calls) == 3


def test_bride_looks_are_hair_edits_of_the_selected_makeup(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        # No makeup selected for the style yet: no hair.
        response = client.post(f"/api/projects/{project['id']}/looks", json={
            'client_request_id': 'early', 'person': 'a', 'style_id': 'city_story', 'items': [{'hairstyle_id': 'keep'}]})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'makeup_required'
        first, second = make_makeups(client, project['id'], 2)
        assert client.post(f'/api/makeups/{second}/select').status_code == 200
        state = makeup_state(client, project['id'])
        assert (state[first]['selected'], state[second]['selected_by']) == (False, 'user')
        stylist = app.state.stylist
        before = len(stylist.calls)
        ids = make_bride_looks(client, project['id'], ['keep', 'low_bun', 'high_bun'])
        hair_calls = stylist.calls[before:]
        # One hair edit per look, each on the selected made-up photo; the makeup is not made again.
        assert len(hair_calls) == 3 and all('这一步只改头发' in prompt for _i, prompt, _a in hair_calls)
        made_up = state[second]['asset_id']
        assert {images[0].stem for images, _p, _a in hair_calls} == {made_up}
        looks = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert {(looks[look_id]['makeup_id'], looks[look_id]['makeup_asset_id']) for look_id in ids} == {(second, made_up)}
        assert all(looks[look_id]['status'] == 'ready' and looks[look_id]['makeup_color'] == 'rose_bean' for look_id in ids)
        # Another style needs its own makeup.
        response = client.post(f"/api/projects/{project['id']}/looks", json={
            'client_request_id': 'other', 'person': 'a', 'style_id': 'forest_film', 'items': [{'hairstyle_id': 'keep'}]})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'makeup_required'
        # Picking another makeup changes only the looks made from now on.
        assert client.post(f'/api/makeups/{first}/select').status_code == 200
        later = make_bride_looks(client, project['id'], ['keep'], request_id='later')[0]
        looks = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert looks[later]['makeup_id'] == first and looks[ids[0]]['makeup_id'] == second


def test_scores_pick_the_most_similar_makeup_and_its_looks_share_its_score(app_factory):
    from app.styling import auto_select_makeup
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper) as (client, app):
        project = create_project(client)
        first, second = make_makeups(client, project['id'], 2)
        wait_until(lambda: makeup_state(client, project['id']), lambda state: all(item['face_score'] is not None for item in state.values()))
        assert len(swapper.scored) == 2
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE makeups SET face_score=? WHERE id=?', (0.41, first))
            conn.execute('UPDATE makeups SET face_score=? WHERE id=?', (0.63, second))
            row = conn.execute('SELECT * FROM makeups WHERE id=?', (first,)).fetchone()
            auto_select_makeup(conn, conn.execute('SELECT * FROM projects WHERE id=?', (project['id'],)).fetchone(), row)
        state = makeup_state(client, project['id'])
        assert (state[first]['selected'], state[second]['selected_by']) == (False, 'auto')
        look_id = make_bride_looks(client, project['id'], ['low_bun'])[0]
        look = wait_until(lambda: next(look for look in detail(client, project['id'])['looks'] if look['id'] == look_id),
                          lambda look: look['face_score'] is not None)
        # The look is scored once; its makeup step's score is the candidate's.
        assert len(swapper.scored) == 3 and look['makeup_face_score'] == 0.63
        # The user's pick stays, whatever the scores say.
        assert client.post(f'/api/makeups/{first}/select').status_code == 200
        with app.state.db.connect(write=True) as conn:
            auto_select_makeup(conn, conn.execute('SELECT * FROM projects WHERE id=?', (project['id'],)).fetchone(), row)
        assert makeup_state(client, project['id'])[first]['selected_by'] == 'user'


def test_deleting_the_selected_makeup_selects_another_and_keeps_its_looks(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        project = create_project(client)
        first, second = make_makeups(client, project['id'], 2)
        selected = next(makeup_id for makeup_id, item in makeup_state(client, project['id']).items() if item['selected'])
        look_id = make_bride_looks(client, project['id'], ['keep'])[0]
        assert client.delete(f'/api/makeups/{selected}').status_code == 204
        state = makeup_state(client, project['id'])
        other = second if selected == first else first
        assert selected not in state and state[other]['selected_by'] == 'auto'
        look = next(look for look in detail(client, project['id'])['looks'] if look['id'] == look_id)
        assert look['makeup_id'] == selected and look['usable']


def test_a_failed_makeup_is_retried_and_a_restart_stops_unfinished_ones(app_factory, tmp_path):
    data_dir = tmp_path / 'makeups'
    stylist = ScriptedGenerator([GeneratorError('rejected')])
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, data_dir=data_dir, concurrency=1) as (client, app):
        project = create_project(client)
        failed, ready = make_makeups(client, project['id'], 2)
        state = makeup_state(client, project['id'])
        assert (state[failed]['status'], state[failed]['error_code']) == ('failed', 'rejected')
        assert state[ready]['selected'] and client.post(f'/api/makeups/{failed}/select').status_code == 409
        assert client.post(f'/api/makeups/{failed}/retry').status_code == 200
        wait_until(lambda: makeup_state(client, project['id']), lambda state: state[failed]['status'] == 'ready')
        assert client.post(f'/api/makeups/{failed}/retry').status_code == 409
        db = app.state.db
    with db.connect(write=True) as conn:
        conn.execute("UPDATE makeups SET status='running',image_asset_id=NULL WHERE id=?", (failed,))
        conn.execute("UPDATE makeups SET status='waiting',image_asset_id=NULL WHERE id=?", (ready,))
    with app_factory(generator=ScriptedGenerator(), data_dir=data_dir) as (client, app):
        state = makeup_state(client, project['id'])
        assert (state[failed]['status'], state[failed]['error_code']) == ('interrupted', 'interrupted_by_restart')
        assert (state[ready]['status'], state[ready]['error_code']) == ('failed', 'not_started')
        assert app.state.stylist.calls == []


def test_makeups_go_with_their_project(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        make_makeups(client, project['id'], 1)
        make_bride_looks(client, project['id'], ['keep'])
        assert client.delete(f"/api/projects/{project['id']}").status_code == 204
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM makeups').fetchone()[0] == 0
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_the_refined_level_is_not_picked_by_likeness(app_factory):
    # V7.10: the refined level narrows the jawline a little; the score would favour the candidate that did the least.
    from app.styling import auto_select_makeup
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'refined'})
        ids = make_makeups(client, project['id'], 2)
        chosen = next(makeup_id for makeup_id, item in makeup_state(client, project['id']).items() if item['selected'])
        other = next(makeup_id for makeup_id in ids if makeup_id != chosen)
        # The other one scores higher, and the first one ready still stays.
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE makeups SET face_score=? WHERE id=?', (0.41, chosen))
            conn.execute('UPDATE makeups SET face_score=? WHERE id=?', (0.63, other))
            row = conn.execute('SELECT * FROM makeups WHERE id=?', (other,)).fetchone()
            auto_select_makeup(conn, conn.execute('SELECT * FROM projects WHERE id=?', (project['id'],)).fetchone(), row)
        state = makeup_state(client, project['id'])
        assert (state[chosen]['selected_by'], state[other]['selected']) == ('auto', False)


def test_another_style_reuses_a_makeup_and_its_looks_without_new_edits(app_factory, monkeypatch):
    """V7.21: a ready makeup of one style can be used by another, with the looks made on it, and no image is made
    again; the shots of the other style describe the makeup the pictures show, not their own style's."""
    from app.prompts import UPDO_STYLES
    # V7.73: no style in use needs an updo since 中式国风 was retired; the reuse into one is checked on a style made to.
    monkeypatch.setitem(UPDO_STYLES, 'flower_house', UPDO_STYLES['chinese_traditional'])
    from app.prompts import MAKEUP_COLOR_BY_ID, STYLE_MAKEUP_FINISH
    from app.styling import find_side_look
    from conftest import preview_batch, wait_batch
    from test_styling import batch_tasks
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        makeup_id = make_makeups(client, project['id'], 1)[0]
        keep, bun = make_bride_looks(client, project['id'], ['keep', 'low_bun'])
        with app.state.db.connect(write=True) as conn:
            # A side look of the bun, as a shot that needed it would have made.
            row = conn.execute('SELECT * FROM looks WHERE id=?', (bun,)).fetchone()
            conn.execute("INSERT INTO looks (id,project_id,person,role,style_id,angle,parent_look_id,hairstyle_id,hair_length,"
                         "makeup_color,makeup_level,source_asset_id,styling_version,status,prompt,image_asset_id,created_at) "
                         "VALUES ('side',?,?,?,?,'left',?,?,?,?,?,?,?,'ready','p',?,?)",
                         (row['project_id'], row['person'], row['role'], row['style_id'], bun, row['hairstyle_id'], row['hair_length'],
                          row['makeup_color'], row['makeup_level'], row['source_asset_id'], row['styling_version'],
                          row['image_asset_id'], row['created_at']))
        calls = len(app.state.stylist.calls)
        state = makeup_state(client, project['id'])
        response = client.post(f'/api/makeups/{makeup_id}/reuse', json={'style_id': 'forest_film'})
        assert response.status_code == 200, response.text
        reused = response.json()
        assert reused['id'] != makeup_id and reused['style_id'] == 'forest_film' and reused['selected_by'] == 'user'
        assert (reused['asset_id'], reused['makeup_color'], reused['makeup_style_id']) == (
            state[makeup_id]['asset_id'], 'rose_bean', 'city_story')
        # The original stays the selected makeup of its own style.
        assert makeup_state(client, project['id'])[makeup_id]['selected']
        looks = detail(client, project['id'])['looks']
        source = {look['id']: look for look in looks}
        copies = [look for look in looks if look['style_id'] == 'forest_film']
        assert sorted(look['hairstyle_id'] for look in copies) == ['keep', 'low_bun']
        assert all(look['makeup_id'] == reused['id'] and look['makeup_style_id'] == 'city_story' and look['usable']
                   for look in copies)
        assert {look['asset_id'] for look in copies} == {source[keep]['asset_id'], source[bun]['asset_id']}
        assert detail(client, project['id'])['style_looks']['forest_film']['bride'] in {look['id'] for look in copies}
        with app.state.db.connect() as conn:
            bun_copy = next(look['id'] for look in copies if look['hairstyle_id'] == 'low_bun')
            # The copy shares the side look of its image (V7.31) rather than getting its own row.
            assert find_side_look(conn, bun_copy, 'left', row['source_asset_id']) == row['image_asset_id']
            assert conn.execute("SELECT COUNT(*) FROM looks WHERE angle='left'").fetchone()[0] == 1
        # Reusing it again adds nothing; an updo style takes only the updo.
        assert client.post(f'/api/makeups/{makeup_id}/reuse', json={'style_id': 'forest_film'}).json()['id'] == reused['id']
        assert client.post(f'/api/makeups/{reused["id"]}/reuse', json={'style_id': 'flower_house'}).status_code == 200
        looks = detail(client, project['id'])['looks']
        assert sum(look['style_id'] == 'forest_film' for look in looks) == 2
        assert [look['hairstyle_id'] for look in looks if look['style_id'] == 'flower_house'] == ['low_bun']
        assert len(app.state.stylist.calls) == calls
        # The sample of the reusing style describes the makeup of the look it is shot from.
        batch = preview_batch(client, project['id'], request_id='reused', style_ids=['forest_film'])
        wait_batch(client, batch['id'])
        paragraph = batch_tasks(client, batch['id'])[0]['prompt'].split('\n\n')[2]
        assert MAKEUP_COLOR_BY_ID['rose_bean'].text in paragraph and STYLE_MAKEUP_FINISH['city_story'] in paragraph
        assert MAKEUP_COLOR_BY_ID['peach_coral'].text not in paragraph
        # A new hairstyle on the reused makeup is a hair edit of its image and wears its makeup too.
        later = make_bride_looks(client, project['id'], ['soft_waves'], style_id='forest_film', request_id='later')[0]
        look = next(look for look in detail(client, project['id'])['looks'] if look['id'] == later)
        assert (look['makeup_id'], look['makeup_asset_id'], look['makeup_style_id']) == (reused['id'], reused['asset_id'], 'city_story')


def test_a_reused_look_shares_side_looks_made_after_the_reuse(app_factory):
    """V7.31: a side look belongs to the front look's image, so the copy another style reuses shows the side looks
    made on the original after the reuse (and the other way round), with no second paid edit; deleting one hides it
    in both styles."""
    from conftest import PNG
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        makeup_id = make_makeups(client, project['id'], 1)[0]
        [bun] = make_bride_looks(client, project['id'], ['low_bun'])
        assert client.post(f'/api/makeups/{makeup_id}/reuse', json={'style_id': 'forest_film'}).status_code == 200
        copy = next(look['id'] for look in detail(client, project['id'])['looks'] if look['style_id'] == 'forest_film')
        for angle in ('left', 'right'):
            client.post(f"/api/projects/{project['id']}/images", data={'person': 'a', 'angle': angle},
                        files={'file': (f'a-{angle}.png', PNG, 'image/png')})
        calls = len(app.state.stylist.calls)
        assert client.post(f'/api/looks/{bun}/side-looks').status_code == 201
        sides = wait_until(lambda: detail(client, project['id'])['side_looks'],
                           lambda sides: len(sides) == 4 and all(side['status'] == 'ready' for side in sides))
        assert len(app.state.stylist.calls) == calls + 2
        # Both styles show the same two pictures.
        assets = lambda look_id: sorted((side['angle'], side['asset_id']) for side in sides if side['parent_look_id'] == look_id)
        assert assets(copy) == assets(bun) and len(assets(copy)) == 2
        # Asking on the copy makes nothing: they are already made.
        assert len(client.post(f'/api/looks/{copy}/side-looks').json()) == 2
        assert len(app.state.stylist.calls) == calls + 2
        # Deleting one in the reusing style deletes it for both; asking again makes it once.
        left = next(side for side in sides if side['parent_look_id'] == copy and side['angle'] == 'left')
        assert client.delete(f"/api/side-looks/{left['id']}").status_code == 204
        assert [side['angle'] for side in detail(client, project['id'])['side_looks']] == ['right', 'right']
        client.post(f'/api/looks/{copy}/side-looks')
        wait_until(lambda: detail(client, project['id'])['side_looks'],
                   lambda sides: len(sides) == 4 and all(side['status'] == 'ready' for side in sides))
        assert len(app.state.stylist.calls) == calls + 3


def test_only_a_usable_makeup_can_be_reused(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        makeup_id = make_makeups(client, project['id'], 1)[0]
        for body, status, code in (({'style_id': 'nowhere'}, 422, 'invalid_style'), ({'style_id': 'black_white'}, 422, 'invalid_style')):
            response = client.post(f'/api/makeups/{makeup_id}/reuse', json=body)
            assert response.status_code == status and response.json()['detail']['code'] == code, response.text
        with app.state.db.connect(write=True) as conn:
            conn.execute("UPDATE makeups SET status='failed' WHERE id=?", (makeup_id,))
        response = client.post(f'/api/makeups/{makeup_id}/reuse', json={'style_id': 'forest_film'})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'makeup_unusable'


def test_the_groom_is_retouched_first_and_his_hairstyles_are_hair_edits_of_it(app_factory):
    """V7.22: the groom's look is two edits too: a retouched photo (hair unchanged, one for every style), then only
    the hair on the selected one."""
    from app.prompts import compose_groom_hair_prompt, compose_groom_retouch_prompt
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        url = f"/api/projects/{project['id']}/looks"
        # No retouched photo selected yet: no hair.
        response = client.post(url, json={'client_request_id': 'early', 'person': 'b', 'items': [{'hairstyle_id': 'keep'}]})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'makeup_required'
        response = client.post(f"/api/projects/{project['id']}/makeups", json={'client_request_id': 'groom', 'person': 'b', 'count': 2})
        assert response.status_code == 201, response.text
        ids = [item['id'] for item in response.json()]
        wait_until(lambda: makeup_state(client, project['id']), lambda state: all(state[i]['status'] == 'ready' for i in ids))
        state = makeup_state(client, project['id'])
        assert all((state[i]['role'], state[i]['style_id'], state[i]['makeup_color']) == ('groom', None, None) for i in ids)
        calls = app.state.stylist.calls
        from app.retouch import retouch_preset
        assert [prompt for _i, prompt, _a in calls] == [compose_groom_retouch_prompt('natural', retouch_preset('natural'))] * 2
        assert all(images[0].stem == project['person_b']['asset_id'] for images, _p, _a in calls)
        assert '头发保持原照片的样子' in calls[0][1] and '发型：' not in calls[0][1]
        selected = next(i for i in ids if state[i]['selected'])
        before = len(calls)
        response = client.post(url, json={'client_request_id': 'hair', 'person': 'b',
                                          'items': [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'side_part'}]})
        assert response.status_code == 201, response.text
        look_ids = [look['id'] for look in response.json()]
        wait_until(lambda: detail(client, project['id'])['looks'],
                   lambda looks: all(look['status'] == 'ready' for look in looks if look['id'] in look_ids))
        hair_calls = calls[before:]
        # One hair edit per look on the selected retouched photo; the retouching is not done again.
        assert sorted(prompt for _i, prompt, _a in hair_calls) == sorted(
            compose_groom_hair_prompt(hairstyle) for hairstyle in ('keep', 'side_part'))
        assert {images[0].stem for images, _p, _a in hair_calls} == {state[selected]['asset_id']}
        looks = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert all((looks[i]['makeup_id'], looks[i]['makeup_asset_id'], looks[i]['style_id']) == (selected, state[selected]['asset_id'], None)
                   for i in look_ids)
        # One look serves every style, and the groom's retouched photo cannot be "reused" for a style.
        assert client.post(f'/api/makeups/{selected}/reuse', json={'style_id': 'forest_film'}).json()['detail']['code'] == 'invalid_person'


def test_a_refined_groom_look_on_a_retouched_photo_is_picked_by_its_score(app_factory):
    """V7.15 kept the score from picking a refined groom look, since each was one edit that also narrowed the jaw;
    since V7.22 his looks share one retouched photo and differ only in the hair, as the bride's do."""
    from app.styling import auto_select
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        client.patch(f"/api/projects/{project['id']}", json={'groom_makeup_level': 'refined'})
        response = client.post(f"/api/projects/{project['id']}/makeups", json={'client_request_id': 'groom', 'person': 'b', 'count': 1})
        makeup_id = response.json()[0]['id']
        wait_until(lambda: makeup_state(client, project['id']), lambda state: state[makeup_id]['status'] == 'ready')
        response = client.post(f"/api/projects/{project['id']}/looks", json={
            'client_request_id': 'hair', 'person': 'b', 'items': [{'hairstyle_id': 'keep'}, {'hairstyle_id': 'side_part'}]})
        first, second = [look['id'] for look in response.json()]
        wait_until(lambda: detail(client, project['id'])['looks'],
                   lambda looks: all(look['status'] == 'ready' for look in looks if look['id'] in (first, second)))
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE looks SET face_score=0.4 WHERE id=?', (first,))
            conn.execute('UPDATE looks SET face_score=0.7 WHERE id=?', (second,))
            row = conn.execute('SELECT * FROM looks WHERE id=?', (first,)).fetchone()
            auto_select(conn, conn.execute('SELECT * FROM projects WHERE id=?', (project['id'],)).fetchone(), row)
        looks = {look['id']: look for look in detail(client, project['id'])['looks']}
        assert (looks[second]['selected'], looks[second]['selected_by']) == (True, 'auto')


@pytest.mark.parametrize('jewelry_id,jewelry_name', [
    ('shell_pearl_drops', '贝壳珍珠耳坠'),
    ('pearl_choker_set', '珍珠耳钉和单串珍珠短项链'),
    ('diamond_necklace_set', '钻石耳钉和细排钻项链'),
    ('jade_drops', '绿色翡翠耳坠'),
    ('diamond_tassel_drops', '细链流苏钻石耳坠'),
    ('pearl_diamond_clusters', '珍珠钻石簇花耳饰'),
])
def test_a_bride_look_wears_only_the_jewelry_picked_for_it(app_factory, jewelry_id, jewelry_name):
    """V7.61: jewelry is a choice of each bride look, none by default; the groom wears none."""
    from app.prompts import JEWELRY_BY_ID
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        first, _second = make_makeups(client, project['id'], 2)
        stylist = app.state.stylist
        before = len(stylist.calls)
        response = client.post(f"/api/projects/{project['id']}/looks", json={
            'client_request_id': 'jewelry', 'person': 'a', 'style_id': 'city_story',
            'items': [{'hairstyle_id': 'keep', 'jewelry_id': 'pearl_studs'}, {'hairstyle_id': 'low_bun'},
                      {'hairstyle_id': 'low_bun', 'jewelry_id': jewelry_id}]})
        assert response.status_code == 201, response.text
        worn, bare, added = [item['id'] for item in response.json()]
        looks = wait_until(lambda: {look['id']: look for look in detail(client, project['id'])['looks']},
                           lambda state: all(state[i]['status'] == 'ready' for i in (worn, bare, added)))
        assert (looks[worn]['jewelry_id'], looks[worn]['jewelry_name']) == ('pearl_studs', '珍珠耳钉')
        assert looks[bare]['jewelry_id'] is None
        assert (looks[added]['jewelry_id'], looks[added]['jewelry_name']) == (jewelry_id, jewelry_name)
        prompts = [prompt for _images, prompt, _a in stylist.calls[before:]]
        assert sum(JEWELRY_BY_ID['pearl_studs'].text in prompt for prompt in prompts) == 1
        assert sum(JEWELRY_BY_ID[jewelry_id].text in prompt for prompt in prompts) == 1
        bad = client.post(f"/api/projects/{project['id']}/looks", json={
            'client_request_id': 'bad', 'person': 'a', 'style_id': 'city_story', 'items': [{'hairstyle_id': 'keep', 'jewelry_id': 'crown'}]})
        assert bad.status_code == 422 and bad.json()['detail']['code'] == 'invalid_jewelry'
        groom = client.post(f"/api/projects/{project['id']}/looks", json={
            'client_request_id': 'groom', 'person': 'b', 'items': [{'hairstyle_id': 'keep', 'jewelry_id': 'pearl_studs'}]})
        assert groom.status_code == 422 and groom.json()['detail']['code'] == 'invalid_jewelry'
