"""Wide plus the four studio framings (V3.14); retired V3.4–V3.11 sizes stay readable on old works."""

import json

from conftest import ScriptedGenerator, create_project, preview_batch, wait_batch
from test_face_swap import ScriptedFaceSwapper


def shoot_mock_set(client, *, request_id='ladder'):
    """Mock director order: 全身 landscape, 七分身, 半身, 近景."""
    project = create_project(client)
    batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
    wait_batch(client, batch['id'])
    preview = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
    draft = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': preview['id'], 'count': 4}).json()
    shot = client.post(f"/api/projects/{project['id']}/photo-batches", json={'client_request_id': request_id, 'draft_id': draft['id']})
    assert shot.status_code == 201, shot.text
    wait_batch(client, shot.json()['id'])
    photos = client.get(f"/api/projects/{project['id']}").json()['photos']
    return project, shot.json()['id'], photos


def make_legacy_detail(app, slot):
    """Turn a finished photo into a detail shot as written by V3.4–V3.9."""
    with app.state.db.connect(write=True) as conn:
        conn.execute("UPDATE photo_slots SET shot_size='detail' WHERE id=?", (slot['id'],))
        conn.execute("UPDATE generation_tasks SET moment_shot_size='detail' WHERE target_slot_id=?", (slot['id'],))


def test_legacy_detail_shots_still_refuse_identity_boost_and_face_swap(app_factory):
    with app_factory(generator=ScriptedGenerator(), face_swapper=ScriptedFaceSwapper(), concurrency=1) as (client, app):
        project, _, photos = shoot_mock_set(client)
        make_legacy_detail(app, photos[0])
        detail = next(photo for photo in client.get(f"/api/projects/{project['id']}").json()['photos'] if photo['id'] == photos[0]['id'])
        assert detail['shot_size'] == 'detail'
        boost = client.post(f"/api/slots/{detail['id']}/regenerate", json={'client_request_id': 'b', 'mode': 'identity_boost'})
        swap = client.post(f"/api/slots/{detail['id']}/face-swap", json={'client_request_id': 's'})
        assert boost.status_code == swap.status_code == 409
        assert boost.json()['detail']['code'] == swap.json()['detail']['code'] == 'no_faces'


def test_change_shot_accepts_wide_and_the_studio_framings_only(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, _app):
        _, _, photos = shoot_mock_set(client)
        target = next(photo for photo in photos if photo['shot_size'] == 'three_quarter')
        for retired_shot in ('medium', 'extreme_close', 'detail'):
            retired = client.post(f"/api/slots/{target['id']}/change-shot", json={'client_request_id': retired_shot, 'shot_size': retired_shot})
            assert retired.status_code == 422
        for shot in ('half', 'wide'):
            changed = client.post(f"/api/slots/{target['id']}/change-shot", json={'client_request_id': shot, 'shot_size': shot})
            assert changed.status_code == 201, changed.text
            # One task per photo at a time (slot_busy): let this one finish before the next change.
            wait_batch(client, changed.json()['id'])


def test_unshot_drafts_keep_wide_and_move_retired_sizes_to_current_framings(app_factory):
    with app_factory(concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        preview = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        draft = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': preview['id'], 'count': 4}).json()
        with app.state.db.connect(write=True) as conn:
            moments = json.loads(conn.execute('SELECT moments FROM moment_drafts WHERE id=?', (draft['id'],)).fetchone()[0])
            for moment, shot in zip(moments, ('wide', 'medium', 'extreme_close', 'detail')):
                moment['shot_size'] = shot
            conn.execute('UPDATE moment_drafts SET moments=? WHERE id=?', (json.dumps(moments, ensure_ascii=False), draft['id']))
        renamed = client.patch(f"/api/moment-drafts/{draft['id']}/moments/0", json={'title': '旧方案改名'})
        assert renamed.status_code == 200, renamed.text
        assert [moment['shot_size'] for moment in renamed.json()['moments']] == ['wide', 'three_quarter', 'close', 'close']


def test_change_shot_keeps_the_scene_of_the_source_photo(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, _app):
        _, batch_id, photos = shoot_mock_set(client, request_id='keep-scene')
        target = next(photo for photo in photos if photo['shot_size'] == 'three_quarter')
        source = next(task for task in client.get('/api/tasks', params={'batch_id': batch_id}).json()
                      if task['target_slot_id'] == target['id'])
        assert '场景：' in source['prompt'].split('\n\n')[2]
        changed = client.post(f"/api/slots/{target['id']}/change-shot", json={'client_request_id': 'keep-scene-close', 'shot_size': 'close'})
        assert changed.status_code == 201, changed.text
        task = next(task for task in client.get('/api/tasks', params={'batch_id': changed.json()['id']}).json())
        assert task['prompt'].split('\n\n')[2] == source['prompt'].split('\n\n')[2]


def test_change_shot_keeps_the_looks_of_the_picture(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'refined'})
        preview_slot = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, preview_slot['id'])
        preview = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        draft = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': preview['id'], 'count': 4}).json()
        shot = client.post(f"/api/projects/{project['id']}/photo-batches", json={'client_request_id': 'makeup-set', 'draft_id': draft['id']})
        wait_batch(client, shot.json()['id'])
        target = next(photo for photo in client.get(f"/api/projects/{project['id']}").json()['photos'] if photo['shot_size'] == 'three_quarter')
        client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'light'})
        changed = client.post(f"/api/slots/{target['id']}/change-shot", json={'client_request_id': 'makeup-close', 'shot_size': 'close'})
        assert changed.status_code == 201, changed.text
        wait_batch(client, changed.json()['id'])
        # V5.0: a new shot keeps the picture's looks; the new level only applies to looks generated later.
        assert len(app.state.stylist.calls) == 2
        task = client.get('/api/tasks', params={'batch_id': changed.json()['id']}).json()[0]
        assert task['prompt'].split('\n\n')[1].startswith('图片角色：第一张图是新娘的造型照')
        changed_slot = next(photo for photo in client.get(f"/api/projects/{project['id']}").json()['photos'] if photo['id'] == target['id'])
        assert changed_slot['look_ids'] == target['look_ids'] == preview['look_ids']
