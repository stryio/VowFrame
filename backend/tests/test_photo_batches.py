"""Photo-set generation contract and in-place database upgrades."""

import json
import sqlite3

from app.db import Database
from conftest import PNG, create_director_photo_batch, create_project, preview_batch, wait_batch


def ready_preview(client, project_id):
    batch = preview_batch(client, project_id, style_ids=['korean_minimal'])
    wait_batch(client, batch['id'])
    return client.get(f'/api/projects/{project_id}').json()['previews'][0]


def test_photo_batch_counts_poses_and_continues_style(client_app):
    client, _ = client_app
    project = create_project(client)
    preview = ready_preview(client, project['id'])
    for count in (4, 8, 12):
        response = create_director_photo_batch(client, project['id'], preview['id'], request_id=f'count-{count}', count=count)
        assert response.status_code == 201, response.text
        batch = response.json()
        wait_batch(client, batch['id'])
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail['stage'] == 'shooting'
    assert len(detail['photos']) == 24
    assert all(photo['pose_id'] is None for photo in detail['photos'])
    second_set = create_director_photo_batch(client, project['id'], preview['id'], request_id='second-style-set', count=4).json()
    wait_batch(client, second_set['id'])
    photos = client.get(f"/api/projects/{project['id']}").json()['photos']
    assert all(photo['pose_id'] is None for photo in photos[24:])
    assert client.get(f"/api/projects/{project['id']}").json()['photo_sets'][0]['slot_count'] == 28


def test_twenty_photo_batch_uses_director_moments_and_photo_set_aggregation(client_app):
    client, app = client_app
    project = create_project(client)
    preview = ready_preview(client, project['id'])
    response = create_director_photo_batch(client, project['id'], preview['id'], request_id='twenty', count=20)
    assert response.status_code == 201, response.text
    batch = response.json()
    wait_batch(client, batch['id'])
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert len(detail['photos']) == 20
    assert all(photo['pose_id'] is None and photo['shot_size'] for photo in detail['photos'])
    prompts = client.get('/api/tasks', params={'batch_id': batch['id']}).json()
    assert len(prompts) == 20
    assert all(task['moment_title'] and task['moment_shot_size'] for task in prompts)
    assert all(task['pose_id'] is None for task in prompts)
    with app.state.db.connect() as conn:
        stored_prompts = [row['prompt'] for row in conn.execute(
            'SELECT prompt FROM generation_tasks WHERE batch_id=? ORDER BY rowid', (batch['id'],)
        )]
    assert len({task['moment_title'] for task in prompts}) == 20
    assert detail['photo_sets'] == [{
        'style_id': 'korean_minimal', 'style_name': '韩系极简', 'slot_count': 20,
        'completed_count': 20, 'batch_ids': [batch['id']],
    }]


def test_photo_sets_group_repeated_style_and_second_style(client_app):
    client, _ = client_app
    project = create_project(client)
    first_preview = ready_preview(client, project['id'])
    first = create_director_photo_batch(client, project['id'], first_preview['id'], request_id='style-one-a').json()
    wait_batch(client, first['id'])
    second = create_director_photo_batch(client, project['id'], first_preview['id'], request_id='style-one-b').json()
    wait_batch(client, second['id'])
    second_preview_batch = preview_batch(client, project['id'], request_id='second-style-preview', style_ids=['french_vintage'])
    wait_batch(client, second_preview_batch['id'])
    french_preview = next(s for s in client.get(f"/api/projects/{project['id']}").json()['previews'] if s['style_id'] == 'french_vintage')
    third = create_director_photo_batch(client, project['id'], french_preview['id'], request_id='style-two').json()
    wait_batch(client, third['id'])
    sets = client.get(f"/api/projects/{project['id']}").json()['photo_sets']
    assert [(item['style_id'], item['slot_count'], item['batch_ids']) for item in sets] == [
        ('korean_minimal', 8, [first['id'], second['id']]), ('french_vintage', 4, [third['id']]),
    ]


def test_photo_batch_reference_modes_stale_and_settings(client_app):
    client, app = client_app
    project = create_project(client)
    preview = ready_preview(client, project['id'])
    assert client.patch('/api/settings', json={'batch_reference_mode': 'with_preview'}).json()['batch_reference_mode'] == 'with_preview'
    batch = create_director_photo_batch(client, project['id'], preview['id'], request_id='with-preview').json()
    wait_batch(client, batch['id'])
    with app.state.db.connect() as conn:
        row = conn.execute('SELECT reference_image_ids,prompt FROM generation_tasks WHERE batch_id=?', (batch['id'],)).fetchone()
        refs = json.loads(row['reference_image_ids'])
        assert refs[:2] == [project['person_a']['asset_id'], project['person_b']['asset_id']]
        assert len(refs) == 3 and refs[2] == preview['current_version']['asset_id']
        assert '第二张图是新郎的身份参考。第三张图是风格参考，只参考它的场景、服装、光线和色调，不参考其中的人脸、长相和构图。' in row['prompt']
    assert client.get(f"/api/projects/{project['id']}").json()['photos'][0]['is_stale'] is False
    client.post(f"/api/projects/{project['id']}/images", data={'person': 'a'}, files={'file': ('replacement.png', PNG)})
    assert client.get(f"/api/projects/{project['id']}").json()['photos'][0]['is_stale'] is True
    invalid = client.patch('/api/settings', json={'batch_reference_mode': 'invalid'})
    assert invalid.status_code == 422 and invalid.json()['detail']['code'] == 'invalid_reference_mode'


def test_photo_batch_uses_side_references_per_moment_and_delete_keeps_asset(client_app):
    client, app = client_app
    project = create_project(client)
    side_ids = {}
    for person, angle in (('a', 'right'), ('b', 'left')):
        response = client.post(f"/api/projects/{project['id']}/images", data={'person': person, 'angle': angle},
                               files={'file': (f'{person}-{angle}.png', PNG, 'image/png')})
        assert response.status_code == 200
        side_ids[person] = response.json()[f'person_{person}'][f'{angle}_asset_id']
    preview = ready_preview(client, project['id'])
    response = create_director_photo_batch(client, project['id'], preview['id'], request_id='side-refs', count=4)
    assert response.status_code == 201, response.text
    wait_batch(client, response.json()['id'])
    with app.state.db.connect() as conn:
        rows = conn.execute('SELECT reference_image_ids,prompt FROM generation_tasks WHERE batch_id=? ORDER BY rowid',
                            (response.json()['id'],)).fetchall()
    refs = [json.loads(row['reference_image_ids']) for row in rows]
    assert refs[1][2] == side_ids['b']
    assert refs[2][2] == side_ids['a']
    assert [len(item) for item in refs] == [2, 3, 4, 2]
    # Mock shots: 回眸相依 has the groom facing left; 相视浅笑 has bride right, groom left.
    assert '第二张图是新郎的身份参考，第三张图是新郎脸朝画面左侧时的样子。' in rows[1]['prompt']
    assert '第三张图是新娘脸朝画面右侧时的样子，第四张图是新郎脸朝画面左侧时的样子。' in rows[2]['prompt']
    replacement = client.post(f"/api/projects/{project['id']}/images", data={'person': 'a', 'angle': 'right'},
                               files={'file': ('a-right-new.png', PNG, 'image/png')})
    assert replacement.status_code == 200
    assert any(photo['is_stale'] for photo in client.get(f"/api/projects/{project['id']}").json()['photos'])
    assert client.delete(f"/api/projects/{project['id']}/images/a/right").status_code == 204
    with app.state.db.connect() as conn:
        assert conn.execute('SELECT 1 FROM image_assets WHERE id=?', (side_ids['a'],)).fetchone() is not None


def test_photo_batch_rejects_capacity_per_offending_shot(client_app):
    client, app = client_app
    project = create_project(client)
    for person, angle in (('a', 'right'), ('b', 'left')):
        response = client.post(f"/api/projects/{project['id']}/images", data={'person': person, 'angle': angle},
                               files={'file': (f'{person}-{angle}.png', PNG, 'image/png')})
        assert response.status_code == 200
    preview = ready_preview(client, project['id'])
    client.patch('/api/settings', json={'batch_reference_mode': 'with_preview'})
    draft = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': preview['id'], 'count': 4}).json()
    patched = client.patch(f"/api/moment-drafts/{draft['id']}/moments/0", json={'bride_facing': 'right', 'groom_facing': 'left'})
    assert patched.status_code == 200, patched.text
    response = client.post(f"/api/projects/{project['id']}/photo-batches", json={
        'client_request_id': 'capacity-reject', 'draft_id': draft['id'], 'model': 'gpt-image-2',
    })
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'invalid_model'
    assert '窗边转圈' in response.json()['detail']['message']
    with app.state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM generation_tasks WHERE batch_id IN (SELECT id FROM generation_batches WHERE client_request_id='capacity-reject')").fetchone()[0] == 0


def test_photo_batch_requires_draft_id_and_is_idempotent(client_app):
    client, _ = client_app
    project = create_project(client)
    missing = client.post(f"/api/projects/{project['id']}/photo-batches", json={
        'client_request_id': 'missing', 'preview_slot_id': 'missing', 'count': 4,
    })
    assert missing.status_code == 422
    preview = ready_preview(client, project['id'])
    draft_response = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': preview['id'], 'count': 4})
    draft_id = draft_response.json()['id']
    first = client.post(f"/api/projects/{project['id']}/photo-batches", json={'client_request_id': 'same', 'draft_id': draft_id})
    second = client.post(f"/api/projects/{project['id']}/photo-batches", json={'client_request_id': 'same', 'draft_id': draft_id})
    assert first.status_code == 201 and second.status_code == 200 and first.json()['id'] == second.json()['id']
    assert client.post(f"/api/projects/{project['id']}/select-style", json={'slot_id': preview['id']}).status_code in (404, 405)


def test_previous_schema_migrates_without_losing_project(tmp_path):
    path = tmp_path / 'legacy.db'
    connection = sqlite3.connect(path)
    connection.executescript('''
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, person_a_image_id TEXT,
          person_a_role TEXT NOT NULL DEFAULT 'bride', person_b_image_id TEXT,
          selected_style_id TEXT, selected_preview_version_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE image_assets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, file_path TEXT NOT NULL, asset_type TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE style_presets (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, prompt_template TEXT NOT NULL, is_builtin INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE generation_batches (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, batch_type TEXT NOT NULL, client_request_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE photo_slots (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, batch_id TEXT NOT NULL, kind TEXT NOT NULL, style_id TEXT NOT NULL, current_version_id TEXT, created_at TEXT NOT NULL);
        CREATE TABLE photo_versions (id TEXT PRIMARY KEY, photo_slot_id TEXT NOT NULL, image_asset_id TEXT NOT NULL, generation_task_id TEXT NOT NULL, version_number INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE generation_tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, batch_id TEXT NOT NULL, target_slot_id TEXT NOT NULL, status TEXT NOT NULL, reference_image_ids TEXT NOT NULL, style_id TEXT NOT NULL, pose_id TEXT, prompt TEXT NOT NULL, model_config_snapshot TEXT NOT NULL, provider_task_id TEXT, output_asset_id TEXT, error_code TEXT, error_message TEXT, auto_retry_count INTEGER NOT NULL DEFAULT 0, retry_of_task_id TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
        CREATE TABLE settings (id INTEGER PRIMARY KEY);
    ''')
    connection.execute("INSERT INTO projects VALUES ('legacy','旧项目',NULL,'bride',NULL,NULL,NULL,'2026-01-01','2026-01-02')")
    connection.commit()
    connection.close()
    Database(path).initialize()
    with sqlite3.connect(path) as upgraded:
        assert upgraded.execute("SELECT name FROM projects WHERE id='legacy'").fetchone()[0] == '旧项目'
        assert {'style_id', 'reference_preview_version_id'} <= {row[1] for row in upgraded.execute('PRAGMA table_info(generation_batches)')}
        assert upgraded.execute("SELECT value FROM app_settings WHERE key='batch_reference_mode'").fetchone()[0] == 'prompt_only'


def test_legacy_batch_reference_mode_migrates_to_app_settings(tmp_path):
    path = tmp_path / 'legacy-settings.db'
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, batch_reference_mode TEXT)")
    connection.execute("INSERT INTO settings VALUES (1, 'with_preview')")
    connection.commit()
    connection.close()

    Database(path).initialize()

    with sqlite3.connect(path) as upgraded:
        assert upgraded.execute("SELECT value FROM app_settings WHERE key='batch_reference_mode'").fetchone()[0] == 'with_preview'
        assert upgraded.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone() is not None
