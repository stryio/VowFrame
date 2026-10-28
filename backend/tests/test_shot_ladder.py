"""Five-step shot ladder plus faceless detail shots."""

import json

from app.prompts import DETAIL_QUALITY_PARAGRAPH, QUALITY_PARAGRAPH, compose_moment_prompt
from conftest import ScriptedGenerator, create_project, preview_batch, wait_batch
from test_face_swap import ScriptedFaceSwapper


def shoot_mock_set(client, *, request_id='ladder'):
    """Mock director order: 全景 landscape, 中景, 局部特写, 近景."""
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


def test_detail_shots_do_not_ask_for_faces_in_the_prompt():
    detail = compose_moment_prompt('korean_minimal', '只拍两人十指相扣的手。', '3:4', shot_size='detail').split('\n\n')
    face = compose_moment_prompt('korean_minimal', '两人相视而笑。', '3:4', shot_size='close').split('\n\n')
    assert detail[4] == DETAIL_QUALITY_PARAGRAPH and '面部曝光' not in detail[4]
    assert face[4] == QUALITY_PARAGRAPH


def test_detail_shots_refuse_identity_boost_and_face_swap(app_factory):
    with app_factory(generator=ScriptedGenerator(), face_swapper=ScriptedFaceSwapper(), concurrency=1) as (client, _app):
        _, _, photos = shoot_mock_set(client)
        detail = next(photo for photo in photos if photo['shot_size'] == 'detail')
        boost = client.post(f"/api/slots/{detail['id']}/regenerate", json={'client_request_id': 'b', 'mode': 'identity_boost'})
        swap = client.post(f"/api/slots/{detail['id']}/face-swap", json={'client_request_id': 's'})
        assert boost.status_code == swap.status_code == 409
        assert boost.json()['detail']['code'] == swap.json()['detail']['code'] == 'no_faces'


def test_automatic_face_swap_skips_detail_shots(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_swap=True, concurrency=1) as (client, _app):
        _, batch_id, photos = shoot_mock_set(client)
        tasks = client.get('/api/tasks', params={'batch_id': batch_id}).json()
        swapped_shots = {task['moment_shot_size'] for task in tasks if task['regenerate_mode'] == 'face_swap'}
        assert len(tasks) == 7 and swapped_shots == {'full', 'medium', 'close'}
        detail = next(photo for photo in photos if photo['shot_size'] == 'detail')
        assert detail['current_version']['source_version_id'] is None  # the generated image stays current


def test_change_shot_accepts_only_the_new_ladder(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, _app):
        _, _, photos = shoot_mock_set(client)
        target = next(photo for photo in photos if photo['shot_size'] == 'medium')
        retired = client.post(f"/api/slots/{target['id']}/change-shot", json={'client_request_id': 'old', 'shot_size': 'half'})
        assert retired.status_code == 422
        wide = client.post(f"/api/slots/{target['id']}/change-shot", json={'client_request_id': 'wide', 'shot_size': 'wide'})
        assert wide.status_code == 201, wide.text


def test_unshot_drafts_with_retired_labels_move_to_medium(app_factory):
    with app_factory(concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        preview = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        draft = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': preview['id'], 'count': 4}).json()
        with app.state.db.connect(write=True) as conn:
            moments = json.loads(conn.execute('SELECT moments FROM moment_drafts WHERE id=?', (draft['id'],)).fetchone()[0])
            moments[0]['shot_size'], moments[1]['shot_size'] = 'three_quarter', 'half'
            conn.execute('UPDATE moment_drafts SET moments=? WHERE id=?', (json.dumps(moments, ensure_ascii=False), draft['id']))
        renamed = client.patch(f"/api/moment-drafts/{draft['id']}/moments/0", json={'title': '旧方案改名'})
        assert renamed.status_code == 200, renamed.text
        assert [moment['shot_size'] for moment in renamed.json()['moments']][:2] == ['medium', 'medium']
