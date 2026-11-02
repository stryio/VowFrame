"""Makeup candidates (V7.8): the bride's makeup for one style is generated as candidates on her uploaded photo,
hair unchanged; one is selected per style (the most similar unless the user picks), and every new bride look of the
style is only a hair edit of that made-up photo."""

from app.generator import GeneratorError
from conftest import ScriptedGenerator, create_project, wait_until
from test_face_swap import ScriptedFaceSwapper


def detail(client, project_id):
    return client.get(f'/api/projects/{project_id}').json()


def make_makeups(client, project_id, count, *, style_id='korean_minimal', request_id=None):
    response = client.post(f'/api/projects/{project_id}/makeups', json={
        'client_request_id': request_id or f'makeup-{style_id}-{count}', 'person': 'a', 'style_id': style_id, 'count': count})
    assert response.status_code == 201, response.text
    ids = [item['id'] for item in response.json()]
    wait_until(lambda: detail(client, project_id)['makeups'],
               lambda makeups: all(item['status'] not in ('waiting', 'running') for item in makeups if item['id'] in ids))
    return ids


def makeup_state(client, project_id):
    return {item['id']: item for item in detail(client, project_id)['makeups']}


def make_bride_looks(client, project_id, hairstyles, *, style_id='korean_minimal', request_id='hair'):
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
        bad = (({'person': 'b', 'style_id': 'korean_minimal', 'count': 1}, 422, 'invalid_person'),  # the groom
               ({'person': 'a', 'style_id': 'nowhere', 'count': 1}, 422, 'invalid_style'),
               ({'person': 'a', 'style_id': 'black_white', 'count': 1}, 422, 'invalid_style'))     # retired
        for body, status, code in bad:
            response = client.post(url, json={'client_request_id': code + str(body), **body})
            assert response.status_code == status and response.json()['detail']['code'] == code, response.text
        assert client.post(url, json={'client_request_id': 'many', 'person': 'a', 'style_id': 'korean_minimal', 'count': 5}).status_code == 422
        ids = make_makeups(client, project['id'], 3)
        calls = app.state.stylist.calls
        assert len(calls) == 3 and all(len(images) == 1 and images[0].stem == project['person_a']['asset_id'] for images, _p, _a in calls)
        assert all('这一步只化妆、修整皮肤和换背景' in prompt and '发型：' not in prompt for _i, prompt, _a in calls)
        assert '新娘妆色：眼影用奶咖棕和米杏色' in calls[0][1]
        state = makeup_state(client, project['id'])
        assert [state[makeup_id]['status'] for makeup_id in ids] == ['ready'] * 3
        assert {state[makeup_id]['makeup_color'] for makeup_id in ids} == {'milk_tea'}
        # Without scores the first one ready is selected for the style.
        assert sorted((state[makeup_id]['selected'], state[makeup_id]['selected_by']) for makeup_id in ids) == [
            (False, None), (False, None), (True, 'auto')]
        # A repeated request creates them once.
        again = client.post(url, json={'client_request_id': 'makeup-korean_minimal-3', 'person': 'a', 'style_id': 'korean_minimal', 'count': 3})
        assert again.status_code == 200 and [item['id'] for item in again.json()] == ids and len(calls) == 3


def test_bride_looks_are_hair_edits_of_the_selected_makeup(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        # No makeup selected for the style yet: no hair.
        response = client.post(f"/api/projects/{project['id']}/looks", json={
            'client_request_id': 'early', 'person': 'a', 'style_id': 'korean_minimal', 'items': [{'hairstyle_id': 'keep'}]})
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
        assert all(looks[look_id]['status'] == 'ready' and looks[look_id]['makeup_color'] == 'milk_tea' for look_id in ids)
        # Another style needs its own makeup.
        response = client.post(f"/api/projects/{project['id']}/looks", json={
            'client_request_id': 'other', 'person': 'a', 'style_id': 'french_vintage', 'items': [{'hairstyle_id': 'keep'}]})
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
