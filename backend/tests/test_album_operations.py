"""Album regeneration, history, deletion and export contract coverage."""

import io
import json
import sqlite3
import zipfile

import pytest

from app.db import Database, new_id
from app.prompts import QUALITY_PARAGRAPH
from conftest import BlockingGenerator, PNG, ScriptedGenerator, create_director_photo_batch, create_project, preview_batch, wait_batch


def ready_preview(client, project_id, style_id='korean_minimal'):
    batch = preview_batch(client, project_id, request_id=f'preview-{new_id()}', style_ids=[style_id])
    wait_batch(client, batch['id'])
    return next(slot for slot in client.get(f'/api/projects/{project_id}').json()['previews']
                if slot['style_id'] == style_id)


def ready_photos(client, project_id, *, style_id='korean_minimal', request_id='photos', count=4, model=None):
    preview = ready_preview(client, project_id, style_id)
    batch = create_director_photo_batch(client, project_id, preview['id'], request_id=request_id, count=count, model=model)
    assert batch.status_code == 201, batch.text
    wait_batch(client, batch.json()['id'])
    return preview, client.get(f'/api/projects/{project_id}').json()['photos']


def add_empty_photo_slot(app, project_id, batch_id, style_id='korean_minimal'):
    slot_id = new_id()
    with app.state.db.connect(write=True) as conn:
        conn.execute(
            'INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,created_at) VALUES (?,?,?,?,?,?)',
            (slot_id, project_id, batch_id, 'photo', style_id, '2026-01-01T00:00:00+00:00'),
        )
    return slot_id


def task_for_batch(client, batch_id):
    tasks = client.get('/api/tasks', params={'batch_id': batch_id}).json()
    assert len(tasks) == 1
    return tasks[0]


def test_regenerate_same_copies_current_version_context_after_photo_replacement(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        _, photos = ready_photos(client, project['id'])
        slot = photos[0]
        original = client.get('/api/tasks', params={'batch_id': slot['batch_id']}).json()
        original_task = next(task for task in original if task['target_slot_id'] == slot['id'])
        with app.state.db.connect() as conn:
            original_row = dict(conn.execute('SELECT * FROM generation_tasks WHERE id=?', (original_task['id'],)).fetchone())

        replacement = client.post(f"/api/projects/{project['id']}/images", data={'person': 'a'},
                                  files={'file': ('replacement.png', PNG, 'image/png')}).json()
        response = client.post(f"/api/slots/{slot['id']}/regenerate", json={
            'client_request_id': 'same-after-replacement', 'mode': 'same',
        })
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        regenerated = task_for_batch(client, response.json()['id'])
        with app.state.db.connect() as conn:
            regenerated_row = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (regenerated['id'],)).fetchone()
        assert regenerated_row['reference_image_ids'] == original_row['reference_image_ids']
        assert regenerated_row['style_id'] == original_row['style_id']
        assert regenerated_row['pose_id'] == original_row['pose_id']
        assert regenerated_row['prompt'] == original_row['prompt']
        assert replacement['person_a']['asset_id'] not in json.loads(regenerated_row['reference_image_ids'])
        assert regenerated_row['regenerate_mode'] == 'same'
        current = next(photo for photo in client.get(f"/api/projects/{project['id']}").json()['photos']
                       if photo['id'] == slot['id'])
        assert current['version_count'] == 2 and current['latest_task']['id'] == regenerated['id']


def test_identity_boost_uses_current_photos_style_preview_and_exact_section_six_rules(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        preview, photos = ready_photos(client, project['id'], style_id='cinematic_light')
        client.patch('/api/settings', json={'batch_reference_mode': 'with_preview'})
        replacement = client.post(f"/api/projects/{project['id']}/images", data={'person': 'a'},
                                  files={'file': ('current.png', PNG, 'image/png')}).json()
        with_faces = next(photo for photo in photos if photo['shot_size'] != 'detail')
        response = client.post(f"/api/slots/{with_faces['id']}/regenerate", json={
            'client_request_id': 'identity-boost', 'mode': 'identity_boost',
        })
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        task = task_for_batch(client, response.json()['id'])
        preview_asset = preview['current_version']['asset_id']
        with app.state.db.connect() as conn:
            row = conn.execute('SELECT reference_image_ids,regenerate_mode FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
        references = json.loads(row['reference_image_ids'])
        assert references == [replacement['person_a']['asset_id'], project['person_b']['asset_id'], preview_asset]
        paragraphs = task['prompt'].split('\n\n')
        assert len(paragraphs) == 6
        assert paragraphs[1].endswith('风格要求与长相冲突时，以长相为准。本次是加强长相的重新生成：两人的长相比风格和动作更重要，宁可画面朴素，也要像本人。')
        assert paragraphs[2].endswith('光线均匀柔和，降低风格化。')
        assert paragraphs[3].endswith('，两人面部正对或接近正对镜头，面部完整、清晰、占画面较大比例。')
        assert paragraphs[4] == QUALITY_PARAGRAPH
        assert paragraphs[5].endswith('剪影、过度高反差、侧脸、低头、头发遮挡眼睛。')
        assert '，正面视角。' not in paragraphs[3]
        assert row['regenerate_mode'] == 'identity_boost'


def test_identity_boost_preview_uses_fixed_moment_and_missing_style_preview_is_409(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        preview = ready_preview(client, project['id'])
        result = client.post(f"/api/slots/{preview['id']}/regenerate", json={
            'client_request_id': 'preview-boost', 'mode': 'identity_boost',
        })
        assert result.status_code == 201, result.text
        wait_batch(client, result.json()['id'])
        task = task_for_batch(client, result.json()['id'])
        assert '镜头：两人并肩站立，新娘挽着新郎的手臂' in task['prompt']
        with app.state.db.connect() as conn:
            references = conn.execute(
                'SELECT reference_image_ids FROM generation_tasks WHERE id=?', (task['id'],)
            ).fetchone()[0]
        assert len(json.loads(references)) == 2

        _, photos = ready_photos(client, project['id'], request_id='photos-without-preview')
        client.patch('/api/settings', json={'batch_reference_mode': 'with_preview'})
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE photo_slots SET current_version_id=NULL WHERE id=?', (preview['id'],))
        with app.state.db.connect() as conn:
            before = (conn.execute('SELECT COUNT(*) FROM generation_batches').fetchone()[0],
                      conn.execute('SELECT COUNT(*) FROM generation_tasks').fetchone()[0])
        missing = client.post(f"/api/slots/{photos[0]['id']}/regenerate", json={
            'client_request_id': 'missing-style-preview', 'mode': 'identity_boost',
        })
        assert missing.status_code == 409
        assert missing.json()['detail']['code'] == 'slot_not_ready'
        assert '样片' in missing.json()['detail']['message']
        assert photos[0]['id'] not in missing.json()['detail']['message']
        with app.state.db.connect() as conn:
            after = (conn.execute('SELECT COUNT(*) FROM generation_batches').fetchone()[0],
                     conn.execute('SELECT COUNT(*) FROM generation_tasks').fetchone()[0])
        assert after == before


def test_regenerate_model_override_validation_busy_and_idempotency(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        _, photos = ready_photos(client, project['id'], model='gpt-image-2.5-flare')
        slot = photos[0]
        url = f"/api/slots/{slot['id']}/regenerate"
        client.patch('/api/settings', json={'default_model': 'gemini-nano-banana-2.1'})
        body = {'client_request_id': 'model-override', 'mode': 'same', 'model': 'gpt-image-2.5-flare'}
        created = client.post(url, json=body)
        assert created.status_code == 201
        wait_batch(client, created.json()['id'])
        task = task_for_batch(client, created.json()['id'])
        assert task['model'] == 'gpt-image-2.5-flare'
        repeated = client.post(url, json=body)
        assert repeated.status_code == 200 and repeated.json()['id'] == created.json()['id']
        inherited = client.post(url, json={'client_request_id': 'model-inherited', 'mode': 'same'})
        assert inherited.status_code == 201
        wait_batch(client, inherited.json()['id'])
        assert task_for_batch(client, inherited.json()['id'])['model'] == 'gpt-image-2.5-flare'
        bad_model = client.post(url, json={
            'client_request_id': 'invalid-model', 'mode': 'same', 'model': 'not-registered',
        })
        assert bad_model.status_code == 422 and bad_model.json()['detail']['code'] == 'invalid_model'
        with app.state.db.connect(write=True) as conn:
            current_task = conn.execute(
                'SELECT task.id FROM photo_versions version JOIN generation_tasks task '
                'ON task.id=version.generation_task_id WHERE version.id=(SELECT current_version_id FROM photo_slots WHERE id=?)',
                (slot['id'],),
            ).fetchone()
            conn.execute('UPDATE generation_tasks SET reference_image_ids=? WHERE id=?',
                         (json.dumps(['a', 'b', 'c', 'd', 'e']), current_task['id']))
        too_many = client.post(url, json={
            'client_request_id': 'too-many-references', 'mode': 'same', 'model': 'gpt-image-2',
        })
        assert too_many.status_code == 422 and too_many.json()['detail']['code'] == 'invalid_model'
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM generation_batches').fetchone()[0] == 4

    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        preview_batch_result = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        assert generator.started.wait(2)
        generator.release.set()
        wait_batch(client, preview_batch_result['id'])
        generator.started.clear()
        generator.release.clear()
        photo_batch_result = create_director_photo_batch(
            client, project['id'], client.get(f"/api/projects/{project['id']}").json()['previews'][0]['id'],
            request_id='blocking-photos',
        )
        assert photo_batch_result.status_code == 201, photo_batch_result.text
        photo_batch = photo_batch_result.json()
        assert generator.started.wait(2)
        running_slot = next(task['target_slot_id'] for task in client.get('/api/tasks', params={'batch_id': photo_batch['id']}).json()
                            if task['status'] == 'running')
        blocked = client.post(f"/api/slots/{running_slot}/regenerate", json={
            'client_request_id': 'busy-slot', 'mode': 'same',
        })
        assert blocked.status_code == 409 and blocked.json()['detail']['code'] == 'slot_busy'
        single_message = blocked.json()['detail']['message']
        assert single_message == '这张照片已有任务正在排队或生成，请等它完成后再操作'
        assert running_slot not in single_message

        detail = client.get(f"/api/projects/{project['id']}").json()
        running_photo_position = next(
            index for index, photo in enumerate(detail['photos'], 1) if photo['id'] == running_slot
        )
        running_photo = detail['photos'][running_photo_position - 1]
        batch_blocked = client.post('/api/slots/batch-action', json={
            'client_request_id': 'batch-busy-slot', 'slot_ids': [running_slot], 'action': 'same',
        })
        assert batch_blocked.status_code == 409
        assert batch_blocked.json()['detail']['code'] == 'slot_busy'
        batch_message = batch_blocked.json()['detail']['message']
        assert f"第 {running_photo_position} 张（{running_photo['pose_name']}）" in batch_message
        assert '已有任务正在排队或生成，请等它完成后再操作' in batch_message
        assert running_slot not in batch_message
        generator.release.set()
        wait_batch(client, photo_batch['id'])


@pytest.mark.parametrize('count', [4, 12])
def test_change_pose_uses_director_moment_for_new_tasks(app_factory, count):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        _, photos = ready_photos(client, project['id'], count=count)
        first = photos[0]
        response = client.post(f"/api/slots/{first['id']}/change-pose", json={'client_request_id': 'change-pose'})
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        task = task_for_batch(client, response.json()['id'])
        assert task['pose_id'] is None
        assert task['moment_title'] and task['moment_shot_size']
        assert task['prompt'].split('\n\n')[3].startswith('镜头：')
        detail = client.get(f"/api/projects/{project['id']}").json()
        changed = next(slot for slot in detail['photos'] if slot['id'] == first['id'])
        assert changed['pose_id'] is None and changed['pose_name'] == task['moment_title']
        versions = client.get(f"/api/slots/{first['id']}/versions").json()
        assert versions[0]['regenerate_mode'] == 'change_pose'
        assert versions[0]['pose_id'] is None and versions[0]['pose_name'] == task['moment_title']
        assert versions[0]['is_current'] is True


def test_batch_action_is_atomic_and_assigns_distinct_director_moments(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        _, photos = ready_photos(client, project['id'])
        selected = photos[:2]
        response = client.post('/api/slots/batch-action', json={
            'client_request_id': 'distinct-poses', 'slot_ids': [slot['id'] for slot in selected],
            'action': 'change_pose',
        })
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        tasks = client.get('/api/tasks', params={'batch_id': response.json()['id']}).json()
        assert len(tasks) == 2 and len({task['moment_title'] for task in tasks}) == 2
        assert all(task['pose_id'] is None for task in tasks)

        empty_slot = add_empty_photo_slot(app, project['id'], photos[0]['batch_id'])
        before = client.get(f"/api/projects/{project['id']}/batches").json()
        with app.state.db.connect() as conn:
            task_count = conn.execute('SELECT COUNT(*) FROM generation_tasks').fetchone()[0]
        rejected = client.post('/api/slots/batch-action', json={
            'client_request_id': 'atomic-failure', 'slot_ids': [selected[0]['id'], empty_slot], 'action': 'same',
        })
        assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'slot_not_ready'
        empty_position = next(
            index for index, slot in enumerate(client.get(f"/api/projects/{project['id']}").json()['photos'], 1)
            if slot['id'] == empty_slot
        )
        empty_message = rejected.json()['detail']['message']
        assert f'第 {empty_position} 张照片尚无生成记录' in empty_message
        assert empty_slot not in empty_message
        after = client.get(f"/api/projects/{project['id']}/batches").json()
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM generation_tasks').fetchone()[0] == task_count
        assert [batch['id'] for batch in after] == [batch['id'] for batch in before]
        empty = client.post('/api/slots/batch-action', json={
            'client_request_id': 'empty-selection', 'slot_ids': [], 'action': 'same',
        })
        assert empty.status_code == 422 and empty.json()['detail']['code'] == 'invalid_slots'


def test_versions_restore_pose_and_foreign_version_rejection(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        _, photos = ready_photos(client, project['id'])
        slot = photos[0]
        same = client.post(f"/api/slots/{slot['id']}/regenerate", json={
            'client_request_id': 'version-two', 'mode': 'same',
        }).json()
        wait_batch(client, same['id'])
        versions = client.get(f"/api/slots/{slot['id']}/versions")
        assert versions.status_code == 200
        versions = versions.json()
        assert [version['version_number'] for version in versions] == [2, 1]
        assert set(versions[0]) == {'id', 'asset_id', 'version_number', 'created_at', 'model', 'pose_id',
                                    'pose_name', 'shot_size', 'regenerate_mode', 'source_version_id', 'is_current',
                                    'face_scores'}
        assert versions[0]['regenerate_mode'] == 'same' and versions[0]['is_current'] is True
        assert versions[1]['regenerate_mode'] is None and versions[1]['is_current'] is False
        restored = client.post(f"/api/slots/{slot['id']}/restore", json={'version_id': versions[1]['id']})
        assert restored.status_code == 200 and restored.json()['current_version']['id'] == versions[1]['id']
        assert restored.json()['pose_id'] is None and restored.json()['pose_name'] == slot['pose_name']
        refreshed = client.get(f"/api/slots/{slot['id']}/versions").json()
        assert [version['is_current'] for version in refreshed] == [False, True]
        foreign = client.post(f"/api/slots/{photos[1]['id']}/restore", json={'version_id': versions[1]['id']})
        assert foreign.status_code == 404 and foreign.json()['detail']['code'] == 'version_not_found'
        restore_message = foreign.json()['detail']['message']
        assert restore_message == '这张照片的版本不存在或不属于该作品位置'
        assert photos[1]['id'] not in restore_message and versions[1]['id'] not in restore_message


def test_delete_photo_removes_version_files_keeps_tasks_and_rejects_preview(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        preview, photos = ready_photos(client, project['id'])
        slot = photos[0]
        regenerated = client.post(f"/api/slots/{slot['id']}/regenerate", json={
            'client_request_id': 'delete-version', 'mode': 'same',
        }).json()
        wait_batch(client, regenerated['id'])
        preview_delete = client.delete(f"/api/slots/{preview['id']}")
        assert preview_delete.status_code == 409
        assert preview_delete.json()['detail']['message'] == f"「{preview['style_name']}」样片不能删除"
        assert preview['id'] not in preview_delete.json()['detail']['message']
        with app.state.db.connect() as conn:
            tasks = [dict(row) for row in conn.execute('SELECT * FROM generation_tasks WHERE target_slot_id=?', (slot['id'],))]
            files = [app.state.config.data_dir / row['file_path'] for row in conn.execute(
                'SELECT asset.file_path FROM photo_versions version JOIN image_assets asset ON asset.id=version.image_asset_id '
                'WHERE version.photo_slot_id=?', (slot['id'],),
            )]
        assert len(tasks) == 2 and all(path.is_file() for path in files)
        with app.state.db.connect(write=True) as conn:
            original = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (tasks[0]['id'],)).fetchone()
            conn.execute("UPDATE generation_tasks SET status='held' WHERE id=?", (tasks[0]['id'],))
            conn.execute(
                'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,'
                'style_id,pose_id,prompt,model_config_snapshot,regenerate_mode,created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (new_id(), original['project_id'], original['batch_id'], original['target_slot_id'], 'waiting',
                 original['reference_image_ids'], original['style_id'], original['pose_id'], original['prompt'],
                 original['model_config_snapshot'], original['regenerate_mode'], '2026-01-01T00:00:00+00:00'),
            )
        deleted = client.delete(f"/api/slots/{slot['id']}")
        assert deleted.status_code == 204
        assert all(not path.exists() for path in files)
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM photo_slots WHERE id=?', (slot['id'],)).fetchone()[0] == 0
            assert conn.execute('SELECT COUNT(*) FROM photo_versions WHERE photo_slot_id=?', (slot['id'],)).fetchone()[0] == 0
            retained = conn.execute('SELECT target_slot_id,output_asset_id FROM generation_tasks WHERE target_slot_id=?', (slot['id'],)).fetchall()
            assert len(retained) == 3 and all(row['output_asset_id'] is None for row in retained)
            assert conn.execute(
                "SELECT COUNT(*) FROM generation_tasks WHERE target_slot_id=? AND status='cancelled'", (slot['id'],)
            ).fetchone()[0] == 2
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert client.post(f"/api/tasks/{tasks[0]['id']}/retry").status_code == 409
        assert client.get(f"/api/projects/{project['id']}").json()['photo_count'] == 3


def test_delete_running_photo_returns_busy_and_cancels_waiting_tasks(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        preview = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        assert generator.started.wait(2)
        generator.release.set()
        wait_batch(client, preview['id'])
        generator.started.clear()
        generator.release.clear()
        preview_slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        batch_result = create_director_photo_batch(client, project['id'], preview_slot['id'], request_id='running-delete')
        assert batch_result.status_code == 201, batch_result.text
        batch = batch_result.json()
        assert generator.started.wait(2)
        task = next(task for task in client.get('/api/tasks', params={'batch_id': batch['id']}).json()
                    if task['status'] == 'running')
        with app.state.db.connect(write=True) as conn:
            source = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
            conn.execute(
                'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,'
                'style_id,pose_id,prompt,model_config_snapshot,regenerate_mode,created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (new_id(), source['project_id'], source['batch_id'], source['target_slot_id'], 'held',
                 source['reference_image_ids'], source['style_id'], source['pose_id'], source['prompt'],
                 source['model_config_snapshot'], source['regenerate_mode'], '2026-01-01T00:00:00+00:00'),
            )
        response = client.delete(f"/api/slots/{task['target_slot_id']}")
        assert response.status_code == 409 and response.json()['detail']['code'] == 'slot_busy'
        delete_message = response.json()['detail']['message']
        assert delete_message == '这张照片已有任务正在排队或生成，请等它完成后再操作'
        assert task['target_slot_id'] not in delete_message
        tasks = client.get('/api/tasks', params={'batch_id': batch['id']}).json()
        assert sum(item['status'] == 'cancelled' for item in tasks) == 1
        assert sum(item['status'] == 'waiting' for item in tasks) == 3
        assert next(item for item in tasks if item['id'] == task['id'])['status'] == 'running'
        generator.release.set()
        wait_batch(client, batch['id'])
        assert any(slot['id'] == task['target_slot_id'] for slot in client.get(f"/api/projects/{project['id']}").json()['photos'])


def test_export_zip_has_album_names_current_versions_only_and_validates_slots(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        project = client.patch(f"/api/projects/{project['id']}", json={'name': '我们的/婚纱照'}).json()
        preview, photos = ready_photos(client, project['id'])
        regenerate = client.post(f"/api/slots/{photos[0]['id']}/regenerate", json={
            'client_request_id': 'export-regenerate', 'mode': 'same',
        }).json()
        wait_batch(client, regenerate['id'])
        with app.state.db.connect() as conn:
            version_files = conn.execute(
                'SELECT version.version_number,asset.file_path FROM photo_versions version '
                'JOIN image_assets asset ON asset.id=version.image_asset_id WHERE version.photo_slot_id=?',
                (photos[0]['id'],),
            ).fetchall()
        version_paths = {row['version_number']: app.state.config.data_dir / row['file_path'] for row in version_files}
        version_paths[1].write_bytes(b'OLD-VERSION')
        version_paths[2].write_bytes(b'CURRENT-VERSION')

        export = client.post(f"/api/projects/{project['id']}/export", json={})
        assert export.status_code == 200 and export.headers['content-type'] == 'application/zip'
        disposition = export.headers['content-disposition']
        assert 'filename="vowframe-export.zip"' in disposition
        assert "filename*=UTF-8''vowframe-%E6%88%91%E4%BB%AC%E7%9A%84_%E5%A9%9A%E7%BA%B1%E7%85%A7.zip" in disposition
        with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
            names = archive.namelist()
            assert len(names) == 4
            assert names[0] == f"01_韩系极简_{photos[0]['pose_name']}.png"
            assert all(name.endswith('.png') for name in names)
            assert archive.read(names[0]) == b'CURRENT-VERSION'
            assert all(archive.read(name) == PNG for name in names[1:])
        assert list(app.state.config.temp_dir.iterdir()) == []

        selected = client.post(f"/api/projects/{project['id']}/export", json={'slot_ids': [photos[2]['id'], photos[0]['id']]})
        assert selected.status_code == 200
        with zipfile.ZipFile(io.BytesIO(selected.content)) as archive:
            assert len(archive.namelist()) == 2
            assert archive.namelist()[0].startswith('01_')
            assert archive.namelist()[1].startswith('02_')

        invalid = client.post(f"/api/projects/{project['id']}/export", json={'slot_ids': [preview['id']]})
        assert invalid.status_code == 422 and invalid.json()['detail']['code'] == 'invalid_slots'
        other = create_project(client)
        other_preview = ready_preview(client, other['id'])
        foreign = client.post(f"/api/projects/{project['id']}/export", json={'slot_ids': [other_preview['id']]})
        assert foreign.status_code == 422 and foreign.json()['detail']['code'] == 'invalid_slots'
        empty_slot = add_empty_photo_slot(app, project['id'], photos[0]['batch_id'])
        no_current = client.post(f"/api/projects/{project['id']}/export", json={'slot_ids': [empty_slot]})
        assert no_current.status_code == 422 and no_current.json()['detail']['code'] == 'invalid_slots'


def test_previous_generation_task_schema_migrates_without_losing_rows_or_versions(tmp_path):
    path = tmp_path / 'previous.db'
    db = Database(path)
    db.initialize()
    project_id, batch_id, slot_id, asset_id, task_id, retry_task_id, version_id = (new_id() for _ in range(7))
    with db.connect(write=True) as conn:
        conn.execute('INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)',
                     (project_id, 'legacy', '2026-01-01', '2026-01-01'))
        conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at) VALUES (?,?,?,?,?)',
                     (asset_id, project_id, 'projects/legacy.png', 'photo', '2026-01-01'))
        conn.execute('INSERT INTO generation_batches (id,project_id,batch_type,client_request_id,created_at,style_id) '
                     'VALUES (?,?,?,?,?,?)', (batch_id, project_id, 'photo', 'legacy-request', '2026-01-01', 'korean_minimal'))
        conn.execute('INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,created_at) VALUES (?,?,?,?,?,?)',
                     (slot_id, project_id, batch_id, 'photo', 'korean_minimal', '2026-01-01'))
        conn.execute(
            'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,'
            'pose_id,prompt,model_config_snapshot,output_asset_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (task_id, project_id, batch_id, slot_id, 'completed', '[]', 'korean_minimal', 'pose_03', 'legacy prompt',
             json.dumps({'model': 'gpt-image-2'}), asset_id, '2026-01-01'),
        )
        conn.execute(
            'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,'
            'pose_id,prompt,model_config_snapshot,retry_of_task_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (retry_task_id, project_id, batch_id, slot_id, 'failed', '[]', 'korean_minimal', 'pose_03',
             'legacy retry prompt', json.dumps({'model': 'gpt-image-2'}), task_id, '2026-01-02'),
        )
        conn.execute('INSERT INTO photo_versions (id,photo_slot_id,image_asset_id,generation_task_id,version_number,'
                     'created_at) VALUES (?,?,?,?,?,?)', (version_id, slot_id, asset_id, task_id, 1, '2026-01-01'))
        conn.execute('UPDATE photo_slots SET current_version_id=? WHERE id=?', (version_id, slot_id))

    # Rebuild the task table into its pre-feature shape, including its old slot cascade.
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA foreign_keys=OFF')
    connection.executescript('''
        CREATE TABLE old_generation_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            batch_id TEXT NOT NULL REFERENCES generation_batches(id) ON DELETE CASCADE,
            target_slot_id TEXT NOT NULL REFERENCES photo_slots(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('waiting','held','running','completed','failed','cancelled','interrupted')),
            reference_image_ids TEXT NOT NULL CHECK(json_valid(reference_image_ids)),
            style_id TEXT NOT NULL REFERENCES style_presets(id), pose_id TEXT,
            prompt TEXT NOT NULL, model_config_snapshot TEXT NOT NULL CHECK(json_valid(model_config_snapshot)),
            provider_task_id TEXT, output_asset_id TEXT REFERENCES image_assets(id), error_code TEXT, error_message TEXT,
            auto_retry_count INTEGER NOT NULL DEFAULT 0 CHECK(auto_retry_count BETWEEN 0 AND 2),
            retry_of_task_id TEXT REFERENCES old_generation_tasks(id), created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
        );
        INSERT INTO old_generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,pose_id,
            prompt,model_config_snapshot,provider_task_id,output_asset_id,error_code,error_message,auto_retry_count,
            retry_of_task_id,created_at,started_at,finished_at)
        SELECT id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,pose_id,prompt,
            model_config_snapshot,provider_task_id,output_asset_id,error_code,error_message,auto_retry_count,
            retry_of_task_id,created_at,started_at,finished_at FROM generation_tasks;
        DROP TABLE generation_tasks;
        ALTER TABLE old_generation_tasks RENAME TO generation_tasks;
    ''')
    connection.commit()
    connection.close()

    db.initialize()
    db.initialize()
    with db.connect() as conn:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(generation_tasks)')}
        slot_foreign_keys = [row for row in conn.execute('PRAGMA foreign_key_list(generation_tasks)')
                             if row['from'] == 'target_slot_id']
        task = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (task_id,)).fetchone()
        retry = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (retry_task_id,)).fetchone()
        version = conn.execute('SELECT * FROM photo_versions WHERE id=?', (version_id,)).fetchone()
        assert 'regenerate_mode' in columns and not slot_foreign_keys
        assert task['target_slot_id'] == slot_id and task['regenerate_mode'] is None
        assert retry['retry_of_task_id'] == task_id and retry['prompt'] == 'legacy retry prompt'
        assert version['generation_task_id'] == task_id
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        conn.execute('DELETE FROM photo_slots WHERE id=?', (slot_id,))
        assert conn.execute('SELECT 1 FROM generation_tasks WHERE id=?', (task_id,)).fetchone() is not None
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []



def test_project_makeup_level_shapes_new_prompts_and_regenerations(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, _app):
        project = create_project(client)
        assert project['makeup_level'] == 'natural' and project['groom_makeup_level'] == 'natural'
        assert project['bride_hair_length'] == 'long' and project['groom_hair_length'] == 'short'
        assert client.patch(f"/api/projects/{project['id']}", json={'groom_hair_length': 'long'}).status_code == 422
        assert client.patch(f"/api/projects/{project['id']}", json={'bride_hair_length': None}).status_code == 422
        hair = client.patch(f"/api/projects/{project['id']}", json={'bride_hair_length': 'short', 'groom_hair_length': 'buzz'})
        assert hair.status_code == 200 and hair.json()['bride_hair_length'] == 'short' and hair.json()['groom_hair_length'] == 'buzz'
        assert client.patch(f"/api/projects/{project['id']}", json={'groom_makeup_level': 'heavy'}).status_code == 422
        assert client.patch(f"/api/projects/{project['id']}", json={'groom_makeup_level': None}).status_code == 422
        assert client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'heavy'}).status_code == 422
        assert client.patch(f"/api/projects/{project['id']}", json={'makeup_level': None}).status_code == 422
        patched = client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'refined', 'groom_makeup_level': 'light'})
        assert patched.status_code == 200 and patched.json()['makeup_level'] == 'refined'
        assert patched.json()['groom_makeup_level'] == 'light'
        preview = ready_preview(client, project['id'])
        first = task_for_batch(client, preview['batch_id'])
        assert '新娘化精致的影楼新娘妆' in first['prompt'] and '新郎不化妆，只修干净' in first['prompt']
        assert '新娘发型（短发）：' in first['prompt'] and '新郎发型（寸头）：' in first['prompt']
        # A later change leaves existing photos alone; boosting likeness tones the photo's makeup down to light.
        client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'natural'})
        result = client.post(f"/api/slots/{preview['id']}/regenerate", json={
            'client_request_id': 'makeup-boost', 'mode': 'identity_boost',
        })
        assert result.status_code == 201, result.text
        wait_batch(client, result.json()['id'])
        boosted = task_for_batch(client, result.json()['id'])
        assert '新娘化清透的新娘妆' in boosted['prompt'] and '精致' not in boosted['prompt'].split('\n\n')[1]
        assert '新郎不化妆，只修干净' in boosted['prompt']
        # 'same' replays the latest prompt as it was, makeup included, even though the project now uses natural.
        same = client.post(f"/api/slots/{preview['id']}/regenerate", json={'client_request_id': 'makeup-same', 'mode': 'same'})
        assert same.status_code == 201, same.text
        wait_batch(client, same.json()['id'])
        assert task_for_batch(client, same.json()['id'])['prompt'] == boosted['prompt']
        # Regenerating a sample shoots it again with the current level.
        again = preview_batch(client, project['id'], request_id='makeup-again', style_ids=['korean_minimal'])
        wait_batch(client, again['id'])
        assert '新娘化标准的婚纱照新娘妆' in task_for_batch(client, again['id'])['prompt']
        assert '新郎不化妆，只修干净' in task_for_batch(client, again['id'])['prompt']
