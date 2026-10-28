"""HTTP contract, persistence and project workflow coverage."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import patch
from uuid import UUID

import pytest

from app.prompts import POSES, STYLES, compose_photo_prompt
from conftest import PNG, BlockingGenerator, create_project, preview_batch, wait_batch


def test_health_styles_and_pose_library(client_app):
    client, _ = client_app
    assert client.get('/api/health').json() == {
        'ok': True, 'generator': 'mock', 'model': 'gpt-image-2',
        'face_swap': {'available': False, 'mode': None, 'reason': '未配置 FaceFusion 安装目录'},
    }
    styles = client.get('/api/styles').json()
    assert [s['id'] for s in styles] == [s['id'] for s in STYLES]
    assert [s['name'] for s in styles] == [
        '韩系极简', '法式复古', '森系自然', '海边日落', '中式国风', '高级光影', '新中式',
        '港风复古', '城市夜景', '花海草原', '冬日雪景', '欧式教堂', '黑白经典', '生活日常',
    ]
    assert [s['description'] for s in styles] == [style['description'] for style in STYLES]
    assert all(set(s) == {'id', 'name', 'description'} for s in styles)
    assert [p['id'] for p in POSES] == [f'pose_{i:02d}' for i in range(1, 13)]
    assert '两人相视而笑' in compose_photo_prompt('korean_minimal', 'pose_02', '3:4')
    assert '不参考其中的人脸、长相和构图' in compose_photo_prompt(
        'korean_minimal', 'pose_02', '3:4', with_preview=True,
    )


def test_project_create_list_patch_role_delete(client_app):
    client, app = client_app
    project = create_project(client, upload=False)
    assert UUID(project['id']).version == 4
    assert len(project['id']) == 32
    assert datetime.fromisoformat(project['created_at']).utcoffset().total_seconds() == 0
    assert project['name'] == '我的婚纱照'
    assert project['stage'] == 'upload'
    assert project['person_a'] == {'asset_id': None, 'role': 'bride'}
    assert project['person_b'] == {'asset_id': None, 'role': 'groom'}
    second = client.post('/api/projects', json={'name': '第二个项目'}).json()
    changed = client.patch(f"/api/projects/{project['id']}", json={'name': '我们的婚纱照', 'person_a_role': 'groom'}).json()
    assert changed['person_a']['role'] == 'groom'
    assert changed['person_b']['role'] == 'bride'
    assert changed['name'] == '我们的婚纱照'
    summaries = client.get('/api/projects').json()
    assert [p['id'] for p in summaries] == [project['id'], second['id']]
    assert set(summaries[0]) == {'id', 'name', 'created_at', 'updated_at', 'stage', 'cover_asset_id', 'photo_count', 'active_task_count', 'has_pending_recovery'}
    assert client.get(f"/api/projects/{project['id']}").json() == changed
    response = client.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 204 and response.content == b''
    assert client.get(f"/api/projects/{project['id']}").status_code == 404
    with app.state.db.connect() as conn:
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


def test_project_delete_commits_rows_before_folder_cleanup(client_app, caplog):
    client, app = client_app
    project = create_project(client)
    folder = app.state.config.projects_dir / project['id']
    with patch('app.api.shutil.rmtree', side_effect=OSError('test cleanup failure')):
        response = client.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 204
    assert folder.exists()
    with app.state.db.connect() as conn:
        assert conn.execute('SELECT 1 FROM projects WHERE id=?', (project['id'],)).fetchone() is None
    assert 'Project folder removal failed' in caplog.text


def test_upload_stage_select_and_replacement_keeps_old_assets(client_app):
    client, app = client_app
    project = create_project(client, upload=False)
    project_id = project['id']
    missing = client.post(f'/api/projects/{project_id}/previews', json={'client_request_id': 'missing'})
    assert missing.status_code == 409 and missing.json()['detail']['code'] == 'photos_missing'
    for person, stage in [('a', 'upload'), ('b', 'style')]:
        response = client.post(f'/api/projects/{project_id}/images', data={'person': person}, files={'file': ('../../fake.jpg', PNG, 'image/png')})
        assert response.status_code == 200
        project = response.json()
        assert project['stage'] == stage
        assert client.get(f"/api/assets/{project[f'person_{person}']['asset_id']}").content == PNG
    old_image = project['person_a']['asset_id']
    batch = preview_batch(client, project_id, style_ids=['korean_minimal'])
    wait_batch(client, batch['id'])
    project = client.get(f'/api/projects/{project_id}').json()
    slot = project['previews'][0]
    assert project['stage'] == 'style'
    assert project['cover_asset_id'] == slot['current_version']['asset_id']
    replacement = client.post(f'/api/projects/{project_id}/images', data={'person': 'a'}, files={'file': ('new.png', PNG)}).json()
    assert replacement['stage'] == 'style'
    assert replacement['previews'][0]['is_stale'] is True
    assert replacement['person_a']['asset_id'] != old_image
    assert client.get(f'/api/assets/{old_image}').content == PNG
    with app.state.db.connect() as conn:
        task = conn.execute('SELECT reference_image_ids FROM generation_tasks WHERE batch_id=?', (batch['id'],)).fetchone()
        assert json.loads(task[0])[0] == old_image


def test_previews_complete_and_regenerate_same_slot(client_app):
    client, app = client_app
    project = create_project(client)
    all_styles = len(STYLES)
    batch = preview_batch(client, project['id'], style_ids=[s['id'] for s in STYLES])
    assert batch['total'] == all_styles
    completed = wait_batch(client, batch['id'])
    assert completed['counts'] == {'waiting': 0, 'held': 0, 'running': 0, 'completed': all_styles, 'failed': 0, 'cancelled': 0, 'interrupted': 0}
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert len(detail['previews']) == all_styles
    assert detail['photo_count'] == 0 and detail['active_task_count'] == 0
    assert [s['style_id'] for s in detail['previews']] == [s['id'] for s in STYLES]
    for slot in detail['previews']:
        assert slot['version_count'] == slot['current_version']['version_number'] == 1
        assert slot['latest_task']['status'] == 'completed'
        assert slot['latest_task']['error_code'] is None
        assert slot['latest_task']['model'] == 'gpt-image-2'
        response = client.get(f"/api/assets/{slot['current_version']['asset_id']}")
        assert response.content == PNG
        assert response.headers['content-type'] == 'image/png'
    original = detail['previews'][0]
    next_batch = preview_batch(client, project['id'], request_id='request-2', style_ids=['korean_minimal'])
    wait_batch(client, next_batch['id'])
    regenerated = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
    assert regenerated['id'] == original['id']
    assert regenerated['version_count'] == regenerated['current_version']['version_number'] == 2
    assert regenerated['current_version']['id'] != original['current_version']['id']
    assert client.get(f"/api/assets/{original['current_version']['asset_id']}").content == PNG
    batches = client.get(f"/api/projects/{project['id']}/batches").json()
    assert [b['id'] for b in batches] == [next_batch['id'], batch['id']]
    tasks = client.get('/api/tasks', params={'project_id': project['id'], 'batch_id': batch['id'], 'status': 'completed'}).json()
    assert len(tasks) == all_styles
    assert tasks == sorted(tasks, key=lambda task: task['created_at'], reverse=True)
    assert all(t['prompt'].split('\n\n')[1].startswith('图片角色：') for t in tasks)
    assert all(t['pose_id'] is None and t['auto_retry_count'] == 0 for t in tasks)
    assert all(set(t) == {'id', 'project_id', 'batch_id', 'target_slot_id', 'status', 'style_id', 'pose_id', 'moment_title', 'moment_shot_size', 'prompt', 'error_code', 'error_message', 'auto_retry_count', 'retry_of_task_id', 'regenerate_mode', 'source_version_id', 'model', 'created_at', 'started_at', 'finished_at'} for t in tasks)
    with app.state.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM photo_versions').fetchone()[0] == all_styles + 1
        assert not list(app.state.config.temp_dir.iterdir())
    folder = app.state.config.projects_dir / project['id']
    assert folder.is_dir()
    assert client.delete(f"/api/projects/{project['id']}").status_code == 204
    assert not folder.exists()
    with app.state.db.connect() as conn:
        for table in ('image_assets', 'photo_slots', 'photo_versions', 'generation_batches', 'generation_tasks'):
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_idempotency_including_concurrent_submissions(client_app):
    client, app = client_app
    project = create_project(client)
    url = f"/api/projects/{project['id']}/previews"
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post(url, json={'client_request_id': 'same-request'}), range(4)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    assert len({r.json()['id'] for r in responses}) == 1
    batch_id = responses[0].json()['id']
    wait_batch(client, batch_id)
    assert client.post(url, json={'client_request_id': 'same-request', 'style_ids': ['forest_natural']}).status_code == 200
    with app.state.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM generation_batches').fetchone()[0] == 1
        # Omitting style_ids samples every built-in style.
        assert conn.execute('SELECT COUNT(*) FROM generation_tasks').fetchone()[0] == len(STYLES)
    other = create_project(client)
    repeated = client.post(f"/api/projects/{other['id']}/previews", json={'client_request_id': 'same-request'})
    assert repeated.status_code == 200
    assert repeated.json()['id'] == batch_id
    assert repeated.json()['project_id'] == project['id']


@pytest.mark.parametrize('role,bride_slot,groom_slot', [('bride', 'person_a', 'person_b'), ('groom', 'person_b', 'person_a')])
def test_bride_reference_first_and_context_is_immutable(app_factory, role, bride_slot, groom_slot):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client, role=role)
        batch = preview_batch(client, project['id'])
        assert generator.started.wait(2)
        with app.state.db.connect() as conn:
            tasks = conn.execute('SELECT * FROM generation_tasks WHERE batch_id=?', (batch['id'],)).fetchall()
            for task in tasks:
                assert json.loads(task['reference_image_ids']) == [project[bride_slot]['asset_id'], project[groom_slot]['asset_id']]
        client.patch(f"/api/projects/{project['id']}", json={'person_a_role': 'groom' if role == 'bride' else 'bride'})
        client.post(f"/api/projects/{project['id']}/images", data={'person': 'a'}, files={'file': ('new.png', PNG)})
        with app.state.db.connect() as conn:
            assert [dict(t) for t in conn.execute('SELECT * FROM generation_tasks WHERE batch_id=?', (batch['id'],))] == [dict(t) for t in tasks]
        generator.release.set()
        wait_batch(client, batch['id'])


def test_slot_not_ready_project_busy_and_single_cancel(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'])
        assert generator.started.wait(2)
        detail = client.get(f"/api/projects/{project['id']}").json()
        assert detail['active_task_count'] == 6
        assert client.post(f"/api/projects/{project['id']}/select-style", json={'slot_id': detail['previews'][0]['id']}).status_code in (404, 405)
        tasks = client.get('/api/tasks', params={'batch_id': batch['id']}).json()
        waiting = next(t for t in tasks if t['status'] == 'waiting')
        assert client.post(f"/api/tasks/{waiting['id']}/cancel").json()['status'] == 'cancelled'
        assert client.post(f"/api/tasks/{waiting['id']}/retry").status_code == 409
        running = next(t for t in tasks if t['status'] == 'running')
        assert client.post(f"/api/tasks/{running['id']}/cancel").status_code == 409
        busy = client.delete(f"/api/projects/{project['id']}")
        assert busy.status_code == 409 and busy.json()['detail']['code'] == 'project_busy'
        progress = client.get(f"/api/batches/{batch['id']}").json()
        assert progress['counts']['cancelled'] == 5 and progress['counts']['running'] == 1
        generator.release.set()
        done = wait_batch(client, batch['id'])
        assert done['counts']['completed'] == 1
        assert client.delete(f"/api/projects/{project['id']}").status_code == 204


@pytest.mark.parametrize('method,path,kwargs,status', [
    ('get', '/api/projects/missing', {}, 404),
    ('get', '/api/batches/missing', {}, 404),
    ('post', '/api/batches/missing/continue', {}, 404),
    ('post', '/api/batches/missing/cancel', {}, 404),
    ('post', '/api/tasks/missing/retry', {}, 404),
    ('post', '/api/tasks/missing/cancel', {}, 404),
    ('get', '/api/assets/missing', {}, 404),
    ('get', '/api/unknown', {}, 404),
    ('post', '/api/health', {}, 405),
    ('post', '/api/projects', {'json': {'name': 1}}, 422),
    ('patch', '/api/projects/missing', {'json': {'person_a_role': 'other'}}, 422),
    ('get', '/api/tasks?status=other', {}, 422),
])
def test_error_envelope(client_app, method, path, kwargs, status):
    client, _ = client_app
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == status
    detail = response.json()['detail']
    assert set(detail) == {'code', 'message'}
    assert any('一' <= c <= '鿿' for c in detail['message'])


def test_invalid_uploads_and_styles(client_app):
    client, _ = client_app
    project = create_project(client)
    url = f"/api/projects/{project['id']}"
    for body in ({'name': None}, {'person_a_role': None}, {'person_b_role': 'bride'}):
        assert client.patch(url, json=body).status_code == 422
    assert client.post(url + '/images', data={'person': 'a'}, files={'file': ('bad.png', b'not an image')}).status_code == 422
    assert client.post(url + '/images', data={'person': 'c'}, files={'file': ('photo.png', PNG)}).status_code == 422
    for style_ids in ([], ['unknown'], ['korean_minimal', 'korean_minimal']):
        assert client.post(url + '/previews', json={'client_request_id': 'invalid', 'style_ids': style_ids}).status_code == 422
    assert client.post(url + '/select-style', json={'slot_id': 'missing'}).status_code in (404, 405)


def test_assets_are_confined_and_missing_files_keep_records(client_app, tmp_path):
    client, app = client_app
    project = create_project(client)
    asset_id = project['person_a']['asset_id']
    outside = tmp_path / 'private.png'
    outside.write_bytes(PNG)
    for path in ('../private.png', str(outside)):
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE image_assets SET file_path=? WHERE id=?', (path, asset_id))
        assert client.get(f'/api/assets/{asset_id}').status_code == 404
    link = app.state.config.data_dir / 'escape.png'
    link.symlink_to(outside)
    with app.state.db.connect(write=True) as conn:
        conn.execute('UPDATE image_assets SET file_path=? WHERE id=?', ('escape.png', asset_id))
    assert client.get(f'/api/assets/{asset_id}').status_code == 404
    with app.state.db.connect(write=True) as conn:
        conn.execute('UPDATE image_assets SET file_path=? WHERE id=?', ('missing.png', asset_id))
    response = client.get(f'/api/assets/{asset_id}')
    assert response.status_code == 404 and response.json()['detail']['code'] == 'asset_missing'
    assert client.get(f"/api/projects/{project['id']}").json()['person_a']['asset_id'] == asset_id
