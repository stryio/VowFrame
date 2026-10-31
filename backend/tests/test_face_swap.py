"""Face-swap API, migration, retry and scheduling behavior."""

import json
import sqlite3
import time

from app.db import Database
from app.config import load_config
from app.face_swap import HELPER_PATH, FaceFusionSwapper, FaceSwapError, FaceSwapResult, MockFaceSwapper, create_face_swapper
from app.task_manager import TaskManager
from app.storage import asset_path
from facefusion_helper.swap import _cuda_face_swapper_used, _face_turn, _facing, _scores, _similarity
from conftest import PNG, BlockingGenerator, ScriptedGenerator, create_project, preview_batch, wait_batch, wait_until


# What the FaceFusion helper reports for a groom turned toward the picture's left.
SCRIPTED_FACES = {
    'bride': {'turn': 0.02, 'facing': 'front', 'model': 'hyperswap_1c_256', 'sources': 1},
    'groom': {'turn': -0.31, 'facing': 'left', 'model': 'hyperswap_1b_256', 'sources': 2},
}


SCRIPTED_SCORES = {'before': {'bride': 0.41, 'groom': 0.38}, 'after': {'bride': 0.72, 'groom': None}}


class ScriptedFaceSwapper:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []
        self.scored = []

    @property
    def status(self):
        return {'available': True, 'mode': 'mock', 'reason': None}

    def swap(self, bride, groom, target, weight):
        self.calls.append((bride, groom, target, weight))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return FaceSwapResult(target.read_bytes(), SCRIPTED_FACES, SCRIPTED_SCORES)

    def score(self, items):
        self.scored.extend(items)
        return [{'bride': 0.55, 'groom': 0.61} for _ in items]


class BlockingAuthGenerator:
    def __init__(self):
        import threading
        self.started = threading.Event()
        self.release = threading.Event()

    def generate_image(self, images, prompt, model, aspect_ratio):
        self.started.set()
        if not self.release.wait(10):
            raise RuntimeError('Test generator was not released')
        from app.generator import GeneratorError
        raise GeneratorError('auth_or_quota')


def ready_preview(client, project_id, style_ids=('korean_minimal',), *, request_id='ready'):
    batch = preview_batch(client, project_id, request_id=request_id, style_ids=list(style_ids))
    wait_batch(client, batch['id'])
    detail = client.get(f'/api/projects/{project_id}').json()
    slots = {slot['style_id']: slot for slot in detail['previews']}
    return batch, slots


def test_health_and_endpoint_validation_unavailable_and_without_current_version(app_factory):
    with app_factory() as (client, _app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'], request_id='ready-busy')
        health = client.get('/api/health').json()['face_swap']
        assert health == {'available': False, 'mode': None, 'reason': '未配置 FaceFusion 安装目录'}
        response = client.post(f"/api/slots/{slots['korean_minimal']['id']}/face-swap", json={'client_request_id': 'unavailable'})
        assert response.status_code == 409
        assert response.json()['detail'] == {'code': 'face_swap_unavailable', 'message': health['reason']}

    generator = ScriptedGenerator([Exception('generation fails')])
    with app_factory(generator=generator, face_swapper=MockFaceSwapper()) as (client, _app):
        project = create_project(client)
        failed = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, failed['id'], status='needs_attention')
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        response = client.post(f"/api/slots/{slot['id']}/face-swap", json={'client_request_id': 'no-version'})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'slot_not_ready'


def test_optional_facefusion_config_and_installation_availability(monkeypatch, tmp_path):
    config_file = tmp_path / 'local.json'
    facefusion_dir = tmp_path / 'facefusion'
    config_file.write_text(json.dumps({'facefusion_dir': str(facefusion_dir)}), encoding='utf-8')
    monkeypatch.setenv('VOWFRAME_LOCAL_CONFIG', str(config_file))
    monkeypatch.setenv('VOWFRAME_DATA_DIR', str(tmp_path / 'isolated-data'))
    monkeypatch.delenv('VOWFRAME_FACE_SWAPPER', raising=False)
    config = load_config()
    assert config.facefusion_dir == facefusion_dir.resolve()
    unavailable = create_face_swapper(config)
    assert unavailable.status == {
        'available': False, 'mode': None,
        'reason': 'FaceFusion 安装目录不完整，须包含 run.sh 和 .venv/bin/python',
    }
    (facefusion_dir / '.venv' / 'bin').mkdir(parents=True)
    (facefusion_dir / 'run.sh').write_text('#!/bin/sh\n', encoding='utf-8')
    (facefusion_dir / '.venv' / 'bin' / 'python').write_text('', encoding='utf-8')
    assert FaceFusionSwapper(facefusion_dir, HELPER_PATH).status == {
        'available': True, 'mode': 'facefusion', 'reason': None,
    }
    monkeypatch.setenv('VOWFRAME_FACE_SWAPPER', 'mock')
    assert isinstance(create_face_swapper(config), MockFaceSwapper)


def test_photos_missing_busy_and_idempotency(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(face_swapper=swapper) as (client, app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'], request_id='ready-first')
        slot = slots['korean_minimal']
        first = client.post(f"/api/slots/{slot['id']}/face-swap", json={'client_request_id': 'same-request'})
        assert first.status_code == 201
        repeated = client.post(f"/api/slots/{slot['id']}/face-swap", json={'client_request_id': 'same-request'})
        assert repeated.status_code == 200 and repeated.json()['id'] == first.json()['id']
        wait_batch(client, first.json()['id'])

        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE projects SET person_a_image_id=NULL,person_b_image_id=NULL WHERE id=?', (project['id'],))
        missing = client.post(f"/api/slots/{slot['id']}/face-swap", json={'client_request_id': 'missing-photos'})
        assert missing.status_code == 409 and missing.json()['detail']['code'] == 'photos_missing'

    blocker = BlockingGenerator()
    with app_factory(generator=ScriptedGenerator(), face_swapper=MockFaceSwapper(), concurrency=1) as (client, app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'], request_id='ready-busy')
        slot = slots['korean_minimal']
        app.state.task_manager.generator = blocker
        regen = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'busy-gen', 'mode': 'same'})
        assert regen.status_code == 201 and blocker.started.wait(2)
        busy = client.post(f"/api/slots/{slot['id']}/face-swap", json={'client_request_id': 'busy-swap'})
        assert busy.status_code == 409
        assert busy.json()['detail']['code'] == 'slot_busy'
        assert slot['id'] not in busy.text
        blocker.release.set()
        wait_batch(client, regen.json()['id'])


def test_face_swap_task_fields_versions_and_source_version_of_a_swap(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(face_swapper=swapper) as (client, app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'], request_id='ready-retry-transient')
        slot_id = slots['korean_minimal']['id']
        original_version = slots['korean_minimal']['current_version']['id']

        first = client.post(f'/api/slots/{slot_id}/face-swap', json={'client_request_id': 'first-face-swap'})
        assert first.status_code == 201
        wait_batch(client, first.json()['id'])
        tasks = client.get('/api/tasks', params={'batch_id': first.json()['id']}).json()
        first_task = tasks[0]
        assert first_task['regenerate_mode'] == 'face_swap'
        assert first_task['source_version_id'] == original_version
        assert first_task['prompt'] == ''
        # The model actually applied per face: bride / groom.
        assert first_task['model'] == 'hyperswap_1c_256/hyperswap_1b_256'
        assert first_task['status'] == 'completed'
        bride, groom = swapper.calls[0][0], swapper.calls[0][1]
        assert bride.front.name.endswith('.png') and groom.front.name.endswith('.png')
        assert (bride.left, bride.right, groom.left, groom.right) == (None, None, None, None)
        assert swapper.calls[0][2].is_file()

        with app.state.db.connect() as conn:
            row = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (first_task['id'],)).fetchone()
            assert json.loads(row['reference_image_ids']) == [
                project['person_a']['asset_id'], project['person_b']['asset_id'], None, None, None, None]
            snapshot = json.loads(row['model_config_snapshot'])
            assert snapshot == {
                'generator': 'mock',
                'model': 'hyperswap_1c_256/hyperswap_1b_256',
                'front_model': 'hyperswap_1c_256',
                'side_model': 'hyperswap_1b_256',
                'side_turn': 0.18,
                'pixel_boost': '512x512',
                'face_mask_types': ['box', 'occlusion'],
                'weight': 1.0,
                'faces': SCRIPTED_FACES,
            }

        current = client.get(f'/api/projects/{project["id"]}').json()['previews'][0]['current_version']
        assert current['source_version_id'] == original_version
        versions = client.get(f'/api/slots/{slot_id}/versions').json()
        assert versions[0]['source_version_id'] == original_version
        second = client.post(f'/api/slots/{slot_id}/face-swap', json={'client_request_id': 'second-face-swap'})
        assert second.status_code == 201
        wait_batch(client, second.json()['id'])
        second_task = client.get('/api/tasks', params={'batch_id': second.json()['id']}).json()[0]
        assert second_task['source_version_id'] == original_version
        with app.state.db.connect() as conn:
            assert conn.execute(
                'SELECT moment_title,moment_shot_size,style_id FROM generation_tasks WHERE id=?', (second_task['id'],)
            ).fetchone() == conn.execute(
                'SELECT moment_title,moment_shot_size,style_id FROM generation_tasks WHERE id=?', (first_task['id'],)
            ).fetchone()


def test_batch_action_face_swap_ignores_model_and_validates_every_slot(app_factory):
    with app_factory(face_swapper=MockFaceSwapper(), concurrency=1) as (client, _app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'], ('korean_minimal', 'french_vintage'))
        response = client.post('/api/slots/batch-action', json={
            'client_request_id': 'batch-swaps',
            'slot_ids': [slots['korean_minimal']['id'], slots['french_vintage']['id']],
            'action': 'face_swap', 'model': 'not-a-generator-model',
        })
        assert response.status_code == 201, response.text
        assert response.json()['total'] == 2
        wait_batch(client, response.json()['id'])
        tasks = client.get('/api/tasks', params={'batch_id': response.json()['id']}).json()
        assert len(tasks) == 2 and all(task['regenerate_mode'] == 'face_swap' for task in tasks)

    generator = ScriptedGenerator([None, Exception('second style fails')])
    with app_factory(generator=generator, face_swapper=MockFaceSwapper(), concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal', 'french_vintage'])
        wait_batch(client, batch['id'], status='needs_attention')
        detail = client.get(f"/api/projects/{project['id']}").json()
        slots_by_style = {slot['style_id']: slot for slot in detail['previews']}
        rejected = client.post('/api/slots/batch-action', json={
            'client_request_id': 'all-or-nothing',
            'slot_ids': [slots_by_style['korean_minimal']['id'], slots_by_style['french_vintage']['id']],
            'action': 'face_swap',
        })
        assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'slot_not_ready'
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT 1 FROM generation_batches WHERE client_request_id=?', ('all-or-nothing',)).fetchone() is None


def test_face_swap_retry_policy_and_direct_validation_failures(app_factory):
    swapper = ScriptedFaceSwapper([FaceSwapError('face_swap_failed')] * 3)
    with app_factory(face_swapper=swapper) as (client, app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'], request_id='ready-retry-roles')
        batch = client.post(f"/api/slots/{slots['korean_minimal']['id']}/face-swap", json={'client_request_id': 'retry-face'})
        wait_batch(client, batch.json()['id'], status='needs_attention')
        task = client.get('/api/tasks', params={'batch_id': batch.json()['id']}).json()[0]
        assert task['status'] == 'failed' and task['error_code'] == 'face_swap_failed'
        assert task['auto_retry_count'] == 2 and len(swapper.calls) == 3
        assert task['error_message'] == '换脸处理失败，请稍后重试'

    swapper = ScriptedFaceSwapper([FaceSwapError('roles_swapped')])
    with app_factory(face_swapper=swapper) as (client, _app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'])
        batch = client.post(f"/api/slots/{slots['korean_minimal']['id']}/face-swap", json={'client_request_id': 'no-auto-retry'})
        wait_batch(client, batch.json()['id'], status='needs_attention')
        task = client.get('/api/tasks', params={'batch_id': batch.json()['id']}).json()[0]
        assert task['status'] == 'failed' and task['error_code'] == 'roles_swapped'
        assert task['auto_retry_count'] == 0 and len(swapper.calls) == 1


def test_cuda_face_swapper_requires_cuda_profiles_for_both_models(tmp_path):
    class ProfiledSession:
        def __init__(self, events):
            self.profile_path = tmp_path / f'profile-{len(list(tmp_path.iterdir()))}.json'
            self.profile_path.write_text(json.dumps(events), encoding='utf-8')

        def end_profiling(self):
            return str(self.profile_path)

    cuda_event = {'args': {'provider': 'CUDAExecutionProvider'}}
    cpu_event = {'args': {'provider': 'CPUExecutionProvider'}}
    front_model = 'hyperswap_1c_256'
    side_model = 'hyperswap_1b_256'
    both = {front_model, side_model}

    assert _cuda_face_swapper_used([
        (front_model + '.onnx', ProfiledSession([cuda_event])),
        (side_model + '.onnx', ProfiledSession([cuda_event])),
    ], both)
    assert not _cuda_face_swapper_used([
        (front_model + '.onnx', ProfiledSession([cuda_event])),
        (side_model + '.onnx', ProfiledSession([cpu_event])),
    ], both)
    assert not _cuda_face_swapper_used([
        (front_model + '.onnx', ProfiledSession([cuda_event])),
    ], both)
    # Both faces front-on: one model ran, and that is the whole expectation.
    assert _cuda_face_swapper_used([
        ('content_analyser.onnx', ProfiledSession([cpu_event])),
        (front_model + '.onnx', ProfiledSession([cuda_event])),
    ], {front_model})


def test_missing_reference_asset_fails_without_retry(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(face_swapper=swapper) as (client, app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'], request_id='missing-reference-source')
        with app.state.db.connect() as conn:
            reference_id = project['person_a']['asset_id']
            row = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (reference_id,)).fetchone()
        asset_path(app.state.config, row['file_path']).unlink()
        batch = client.post(
            f"/api/slots/{slots['korean_minimal']['id']}/face-swap",
            json={'client_request_id': 'reference-missing'},
        )
        wait_batch(client, batch.json()['id'], status='needs_attention')
        task = client.get('/api/tasks', params={'batch_id': batch.json()['id']}).json()[0]
        assert task['status'] == 'failed' and task['error_code'] == 'reference_missing'
        assert task['auto_retry_count'] == 0 and swapper.calls == []


def test_face_swap_lane_runs_while_generation_lane_is_full(app_factory):
    blocker = BlockingGenerator()
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, concurrency=1) as (client, app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'], ('korean_minimal',))
        existing = slots['korean_minimal']
        app.state.task_manager.generator = blocker
        generating = preview_batch(client, project['id'], request_id='generation-blocked', style_ids=['french_vintage'])
        assert blocker.started.wait(2)
        app.state.task_manager._backoff_until = time.monotonic() + 30

        face_batch = client.post(f"/api/slots/{existing['id']}/face-swap", json={'client_request_id': 'swap-alongside'})
        assert face_batch.status_code == 201
        finished = wait_batch(client, face_batch.json()['id'])
        assert finished['counts']['completed'] == 1
        assert blocker.started.is_set() and not blocker.release.is_set()
        blocker.release.set()
        wait_batch(client, generating['id'])


def test_auth_quota_hold_does_not_hold_face_swap_tasks_in_same_batch(app_factory):
    swapper = ScriptedFaceSwapper()
    generator = ScriptedGenerator()
    with app_factory(generator=generator, face_swapper=swapper, concurrency=1) as (client, app):
        project = create_project(client)
        baseline, slots = ready_preview(client, project['id'], ('french_vintage', 'forest_natural'), request_id='baseline-for-hold')
        blocking = BlockingAuthGenerator()
        app.state.task_manager.generator = blocking
        current_batch = preview_batch(
            client, project['id'], request_id='batch-with-face-swap',
            style_ids=['french_vintage', 'forest_natural'],
        )
        assert blocking.started.wait(2)
        pending = client.get('/api/tasks', params={'batch_id': current_batch['id']}).json()
        face_task = next(task for task in pending if task['target_slot_id'] == slots['forest_natural']['id'])
        baseline_version = slots['forest_natural']['current_version']['id']
        with app.state.db.connect(write=True) as conn:
            conn.execute(
                'UPDATE generation_tasks SET regenerate_mode=?,source_version_id=?,reference_image_ids=?,prompt=?,model_config_snapshot=? WHERE id=?',
                ('face_swap', baseline_version,
                 json.dumps([project['person_a']['asset_id'], project['person_b']['asset_id']]), '',
                 json.dumps({
                     'generator': 'mock',
                     'model': 'hyperswap_1c_256/hyperswap_1b_256',
                     'front_model': 'hyperswap_1c_256',
                     'side_model': 'hyperswap_1b_256',
                     'side_turn': 0.18,
                     'pixel_boost': '512x512',
                     'face_mask_types': ['box', 'occlusion'],
                     'weight': 1.0,
                 }),
                 face_task['id']),
            )
        blocking.release.set()
        final = wait_batch(client, current_batch['id'], status='needs_attention')
        updated = client.get('/api/tasks', params={'batch_id': current_batch['id']}).json()
        face_task = next(task for task in updated if task['id'] == face_task['id'])
        assert final['counts']['failed'] == 1 and final['counts']['completed'] == 1
        assert face_task['status'] == 'completed'


def test_startup_recovery_treats_face_swap_tasks_as_local(app_factory):
    with app_factory(face_swapper=MockFaceSwapper(), concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal', 'french_vintage', 'forest_natural', 'seaside_sunset', 'chinese_traditional', 'cinematic_light'])
        wait_batch(client, batch['id'])
        tasks = client.get('/api/tasks', params={'batch_id': batch['id']}).json()
        manager = app.state.task_manager
        config, db, generator, swapper = app.state.config, app.state.db, app.state.generator, app.state.face_swapper
    with db.connect(write=True) as conn:
        current_version = conn.execute('SELECT id FROM photo_versions ORDER BY version_number LIMIT 1').fetchone()['id']
        for task, status in zip(tasks[:4], ('running', 'waiting', 'running', 'waiting')):
            is_swap = task in tasks[:2]
            conn.execute(
                'UPDATE generation_tasks SET status=?,regenerate_mode=?,source_version_id=? WHERE id=?',
                (status, 'face_swap' if is_swap else None, current_version if is_swap else None, task['id']),
            )
    TaskManager(config, db, generator, swapper).recover()
    with db.connect() as conn:
        statuses = {
            row['id']: (row['status'], row['error_code'])
            for row in conn.execute('SELECT id,status,error_code FROM generation_tasks')
        }
    assert statuses[tasks[0]['id']] == ('waiting', None)
    assert statuses[tasks[1]['id']] == ('waiting', None)
    assert statuses[tasks[2]['id']] == ('interrupted', 'interrupted_by_restart')
    assert statuses[tasks[3]['id']] == ('held', None)


def test_previous_task_schema_migrates_face_swap_fields_and_preserves_indexes(app_factory):
    with app_factory(face_swapper=MockFaceSwapper(), concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        db = app.state.db
    with sqlite3.connect(db.path) as raw:
        raw.execute('PRAGMA foreign_keys=OFF')
        previous_columns = (
            'id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,pose_id,moment_title,moment_shot_size,'
            'prompt,model_config_snapshot,provider_task_id,output_asset_id,error_code,error_message,auto_retry_count,'
            'retry_of_task_id,regenerate_mode,created_at,started_at,finished_at'
        )
        previous_rows = raw.execute(f'SELECT {previous_columns} FROM generation_tasks').fetchall()
        raw.execute('DROP TABLE generation_tasks')
        raw.execute('''CREATE TABLE generation_tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            batch_id TEXT NOT NULL REFERENCES generation_batches(id) ON DELETE CASCADE,
            target_slot_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('waiting','held','running','completed','failed','cancelled','interrupted')),
            reference_image_ids TEXT NOT NULL CHECK(json_valid(reference_image_ids)),
            style_id TEXT NOT NULL REFERENCES style_presets(id), pose_id TEXT,
            moment_title TEXT, moment_shot_size TEXT,
            prompt TEXT NOT NULL, model_config_snapshot TEXT NOT NULL CHECK(json_valid(model_config_snapshot)),
            provider_task_id TEXT, output_asset_id TEXT REFERENCES image_assets(id),
            error_code TEXT, error_message TEXT,
            auto_retry_count INTEGER NOT NULL DEFAULT 0 CHECK(auto_retry_count BETWEEN 0 AND 2),
            retry_of_task_id TEXT REFERENCES generation_tasks(id),
            regenerate_mode TEXT CHECK(regenerate_mode IS NULL OR regenerate_mode IN ('same','identity_boost','change_pose','change_shot')),
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
        )''')
        raw.executemany(
            'INSERT INTO generation_tasks VALUES (' + ','.join('?' for _ in range(len(previous_columns.split(',')))) + ')',
            [tuple(row) for row in previous_rows],
        )
        raw.commit()
    db.initialize()
    with db.connect() as conn:
        task = conn.execute('SELECT * FROM generation_tasks').fetchone()
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(generation_tasks)')}
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='generation_tasks'").fetchone()['sql']
        indexes = {row['name'] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='generation_tasks'")}
        foreign_keys = {row['from'] for row in conn.execute('PRAGMA foreign_key_list(generation_tasks)')}
    assert task['id'] == previous_rows[0][0]
    assert {'source_version_id', 'regenerate_mode'} <= columns
    assert 'face_swap' in sql and 'source_version_id' not in foreign_keys
    assert {'task_queue', 'task_batch', 'task_project', 'task_slot', 'task_retry_once'} <= indexes


def test_automatic_face_swap_is_on_by_default_for_a_new_database(tmp_path):
    db = Database(tmp_path / 'fresh.db')
    db.initialize()
    assert db.get_auto_face_swap() is True


def test_generated_images_are_face_swapped_automatically_and_keep_the_original(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_swap=True, concurrency=1) as (client, app):
        project = create_project(client)
        batch, slots = ready_preview(client, project['id'])
        # The batch finishes only after the swap: one generation task plus its automatic swap.
        finished = client.get(f"/api/batches/{batch['id']}").json()
        assert finished['total'] == 2 and finished['counts']['completed'] == 2
        slot = slots['korean_minimal']
        versions = client.get(f"/api/slots/{slot['id']}/versions").json()
        swapped, original = versions
        assert swapped['is_current'] and swapped['source_version_id'] == original['id']
        assert original['source_version_id'] is None  # the untouched original stays in the history
        assert len(swapper.calls) == 1
        with app.state.db.connect() as conn:
            task = conn.execute("SELECT * FROM generation_tasks WHERE regenerate_mode='face_swap'").fetchone()
        assert json.loads(task['model_config_snapshot'])['weight'] == 1.0  # samples are swapped at full strength
        assert swapper.calls[0][3] == 1.0
        assert task['batch_id'] == batch['id']


def test_album_photos_are_swapped_at_half_strength_and_samples_at_full_strength(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_swap=True, concurrency=1) as (client, app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'])
        draft = client.post(f"/api/projects/{project['id']}/moment-drafts",
                            json={'preview_slot_id': slots['korean_minimal']['id'], 'count': 4}).json()
        shot = client.post(f"/api/projects/{project['id']}/photo-batches", json={'client_request_id': 'weights', 'draft_id': draft['id']})
        wait_batch(client, shot.json()['id'])
        photo = client.get(f"/api/projects/{project['id']}").json()['photos'][0]
        manual = client.post(f"/api/slots/{photo['id']}/face-swap", json={'client_request_id': 'manual'})
        wait_batch(client, manual.json()['id'])
        with app.state.db.connect() as conn:
            weights = {(kind, json.loads(snapshot)['weight']) for kind, snapshot in conn.execute(
                "SELECT slot.kind, task.model_config_snapshot FROM generation_tasks task "
                "JOIN photo_slots slot ON slot.id=task.target_slot_id WHERE task.regenerate_mode='face_swap'")}
        assert weights == {('preview', 1.0), ('photo', 0.5)}
        assert sorted(call[3] for call in swapper.calls) == [0.5] * 5 + [1.0]


def test_actions_on_a_swapped_image_start_from_its_generation_prompt(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, face_swapper=ScriptedFaceSwapper(), auto_face_swap=True, concurrency=1) as (client, _app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'])
        slot_id = slots['korean_minimal']['id']
        boost = client.post(f'/api/slots/{slot_id}/regenerate', json={'client_request_id': 'boost', 'mode': 'identity_boost'})
        assert boost.status_code == 201, boost.text
        wait_batch(client, boost.json()['id'])
        tasks = client.get('/api/tasks', params={'batch_id': boost.json()['id']}).json()
        boost_task = next(task for task in tasks if task['regenerate_mode'] == 'identity_boost')
        assert boost_task['prompt'].split('\n\n')[3].startswith('镜头：两人并肩站立')
        assert generator.calls[-1][1] == boost_task['prompt']
        # The boosted image is swapped again automatically.
        assert {task['regenerate_mode'] for task in tasks} == {'identity_boost', 'face_swap'}


def test_no_automatic_swap_when_disabled_or_unavailable(app_factory, tmp_path):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_swap=True, concurrency=1) as (client, _app):
        assert client.patch('/api/settings', json={'auto_face_swap': False}).json()['auto_face_swap'] is False
        project = create_project(client)
        batch, _ = ready_preview(client, project['id'])
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 1
        assert swapper.calls == []
    # Without a configured FaceFusion the setting has no effect instead of queuing failing tasks.
    with app_factory(generator=ScriptedGenerator(), auto_face_swap=True, concurrency=1,
                     data_dir=tmp_path / 'without-facefusion') as (client, _app):
        project = create_project(client)
        batch, _ = ready_preview(client, project['id'])
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 1


def test_face_turn_and_facing_follow_the_picture_direction():
    class Face:
        def __init__(self, nose_x):
            # Eyes at x=40 and x=60 (distance 20); the nose moves along the eye line.
            self.landmark_set = {'5/68': [(40, 50), (60, 50), (nose_x, 62), (43, 72), (57, 72)]}

    assert _face_turn(Face(50)) == 0
    assert _face_turn(Face(44)) == -0.3
    assert _face_turn(Face(56)) == 0.3
    assert [_facing(turn, 0.18) for turn in (-0.3, -0.18, -0.17, 0, 0.17, 0.18, 0.3)] == [
        'left', 'left', 'front', 'front', 'front', 'right', 'right']


def test_face_swap_passes_side_photos_by_role_and_slot(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(face_swapper=swapper) as (client, app):
        project = create_project(client, role='groom')  # person a is the groom
        uploads = {}
        for person, angle in (('a', 'left'), ('b', 'right')):
            detail = client.post(f"/api/projects/{project['id']}/images", data={'person': person, 'angle': angle},
                                 files={'file': (f'{person}-{angle}.png', PNG, 'image/png')}).json()
            uploads[(person, angle)] = detail[f'person_{person}'][f'{angle}_asset_id']
        _, slots = ready_preview(client, project['id'], request_id='side-swap-ready')
        batch = client.post(f"/api/slots/{slots['korean_minimal']['id']}/face-swap", json={'client_request_id': 'side-swap'})
        wait_batch(client, batch.json()['id'])
        task = client.get('/api/tasks', params={'batch_id': batch.json()['id']}).json()[0]
        with app.state.db.connect() as conn:
            references = json.loads(conn.execute(
                'SELECT reference_image_ids FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()[0])
        # [bride front, groom front, bride left, bride right, groom left, groom right]
        assert references == [project['person_b']['asset_id'], project['person_a']['asset_id'],
                              None, uploads[('b', 'right')], uploads[('a', 'left')], None]
        bride, groom = swapper.calls[-1][0], swapper.calls[-1][1]
        assert bride.left is None and bride.right is not None
        assert groom.left is not None and groom.right is None


def test_back_view_and_silhouette_shots_are_not_swapped_automatically(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_swap=True, concurrency=1) as (client, _app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'])
        swapper.calls.clear()
        draft = client.post(f"/api/projects/{project['id']}/moment-drafts",
                            json={'preview_slot_id': slots['korean_minimal']['id'], 'count': 4}).json()
        for index, text in ((0, '35mm 镜头，远景。两人牵手走向远处，留下背影。'), (1, '逆光下两人相拥，形成剪影。')):
            patched = client.patch(f"/api/moment-drafts/{draft['id']}/moments/{index}",
                                   json={'text': text, 'faces_clear': False, 'bride_facing': 'front', 'groom_facing': 'front'})
            assert patched.status_code == 200, patched.text
        # Saying there is no silhouette does not make a shot an atmosphere shot.
        patched = client.patch(f"/api/moment-drafts/{draft['id']}/moments/2", json={'text': '两人看向镜头浅笑，不拍剪影。'})
        assert patched.status_code == 200, patched.text
        batch = client.post(f"/api/projects/{project['id']}/photo-batches",
                            json={'client_request_id': 'faceless', 'draft_id': draft['id']}).json()
        wait_batch(client, batch['id'])
        assert len(swapper.calls) == 2  # only the two shots with faces


def test_a_swap_records_the_face_similarity_before_and_after(app_factory):
    with app_factory(generator=ScriptedGenerator(), face_swapper=ScriptedFaceSwapper(), auto_face_swap=True,
                     concurrency=1) as (client, _app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'])
        swapped, original = client.get(f"/api/slots/{slots['korean_minimal']['id']}/versions").json()
        assert original['face_scores'] == SCRIPTED_SCORES['before']
        assert swapped['face_scores'] == SCRIPTED_SCORES['after']  # null: the groom's face was not found
        assert slots['korean_minimal']['current_version']['face_scores'] == SCRIPTED_SCORES['after']


def test_a_picture_that_is_not_swapped_is_scored_on_its_own(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, concurrency=1) as (client, _app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'])
        slot_id = slots['korean_minimal']['id']
        version = wait_until(lambda: client.get(f'/api/slots/{slot_id}/versions').json()[0],
                             lambda value: value['face_scores'] is not None)
        assert version['face_scores'] == {'bride': 0.55, 'groom': 0.61}
        assert swapper.calls == []
        # Compared with the photos the picture was made from, bride first.
        item = swapper.scored[0]
        assert item.bride.stem == project['person_a']['asset_id'] and item.groom.stem == project['person_b']['asset_id']


def test_pictures_without_a_score_are_scored_after_a_restart(app_factory, tmp_path):
    data_dir = tmp_path / 'restart'
    with app_factory(generator=ScriptedGenerator(), face_swapper=MockFaceSwapper(), concurrency=1,
                     data_dir=data_dir) as (client, _app):
        project = create_project(client)
        _, slots = ready_preview(client, project['id'])  # the mock swapper cannot measure faces
    swapper = ScriptedFaceSwapper()
    with app_factory(face_swapper=swapper, data_dir=data_dir) as (client, _app):
        slot_id = slots['korean_minimal']['id']
        version = wait_until(lambda: client.get(f'/api/slots/{slot_id}/versions').json()[0],
                             lambda value: value['face_scores'] is not None)
        assert version['face_scores'] == {'bride': 0.55, 'groom': 0.61}


def test_similarity_compares_each_role_with_its_own_photo():
    class Face:
        def __init__(self, gender, embedding, size=10):
            self.gender, self.embedding, self.size = gender, embedding, size

    def order(faces, _direction):
        return sorted(faces, key=lambda face: face.size, reverse=True)

    bride, groom = Face('female', [1.0, 0.0]), Face('male', [0.0, 2.0])
    assert _similarity(Face('female', [3.0, 0.0]), bride) == 1.0
    assert _similarity(Face('female', [1.0, 1.0]), bride) == 0.707
    assert _similarity(None, bride) is None
    picture = [Face('female', [1.0, 1.0], 30), Face('female', [1.0, 0.0], 5), Face('male', [0.0, -1.0])]
    # The largest face of each gender is the person; a missing face scores None.
    assert _scores(picture, {'bride': bride, 'groom': groom}, order) == {'bride': 0.707, 'groom': -1.0}
    assert _scores(picture[:2], {'bride': bride, 'groom': groom}, order) == {'bride': 0.707, 'groom': None}
