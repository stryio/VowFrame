"""Task outcomes, cancellation, recovery, durability and scheduling invariants."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from app.config import Config
from app.generator import GeneratorError
from app.storage import asset_path
from app.task_manager import TaskManager
from app.prompts import STYLES
from conftest import PNG, BlockingGenerator, ScriptedGenerator, create_project, preview_batch, wait_batch, wait_until


def batch_tasks(client, batch_id):
    return client.get('/api/tasks', params={'batch_id': batch_id}).json()


@pytest.mark.parametrize('kind,status', [
    ('rejected', 'failed'), ('unknown_outcome', 'interrupted'), ('not_sent', 'failed'),
])
def test_error_mapping_and_only_safe_requests_auto_retry(app_factory, kind, status):
    generator = ScriptedGenerator([GeneratorError(kind)] * 3)
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        result = wait_batch(client, batch['id'], status='needs_attention')
        task = batch_tasks(client, batch['id'])[0]
        assert result['counts'][status] == 1
        assert task['status'] == status and task['error_code'] == kind
        assert task['started_at'] is not None and task['finished_at'] is not None
        assert task['auto_retry_count'] == (2 if kind == 'not_sent' else 0)
        assert len(generator.calls) == (3 if kind == 'not_sent' else 1)
        before = list(generator.calls)
        client.post(f"/api/batches/{batch['id']}/continue")
        time.sleep(.05)
        assert generator.calls == before
        detail = client.get(f"/api/projects/{project['id']}").json()
        assert detail['has_pending_recovery'] == (status == 'interrupted')
        assert detail['previews'][0]['current_version'] is None


def test_safe_auto_retry_can_succeed_without_new_task(app_factory):
    generator = ScriptedGenerator([GeneratorError('not_sent'), GeneratorError('not_sent')])
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        tasks = batch_tasks(client, batch['id'])
        assert len(tasks) == 1
        assert tasks[0]['auto_retry_count'] == 2
        assert tasks[0]['error_code'] is None and tasks[0]['error_message'] is None
        assert len(generator.calls) == 3


@pytest.mark.parametrize('kind', ['rejected', 'unknown_outcome'])
def test_manual_retry_copies_context_and_preserves_original(app_factory, kind):
    generator = ScriptedGenerator([GeneratorError(kind)])
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        with app.state.db.connect(write=True) as conn:
            conn.execute(
                'UPDATE generation_tasks SET prompt=? WHERE batch_id=?',
                ('旧版提示词快照，专业婚纱摄影效果。', batch['id']),
            )
        original = batch_tasks(client, batch['id'])[0]
        with app.state.db.connect() as conn:
            original_row = dict(conn.execute('SELECT * FROM generation_tasks WHERE id=?', (original['id'],)).fetchone())
        # Retrying must use the original input snapshot, even after roles/photos change.
        client.patch(f"/api/projects/{project['id']}", json={'person_a_role': 'groom'})
        client.post(f"/api/projects/{project['id']}/images", data={'person': 'a'}, files={'file': ('new.png', PNG)})
        retry = client.post(f"/api/tasks/{original['id']}/retry")
        assert retry.status_code == 201
        new_task = retry.json()
        assert new_task['id'] != original['id']
        assert new_task['retry_of_task_id'] == original['id']
        assert new_task['auto_retry_count'] == 0
        for field in ('project_id', 'batch_id', 'target_slot_id', 'style_id', 'pose_id', 'prompt'):
            assert new_task[field] == original[field]
        wait_until(lambda: batch_tasks(client, batch['id']), lambda tasks: any(t['id'] == new_task['id'] and t['status'] == 'completed' for t in tasks))
        with app.state.db.connect() as conn:
            assert dict(conn.execute('SELECT * FROM generation_tasks WHERE id=?', (original['id'],)).fetchone()) == original_row
            row = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (new_task['id'],)).fetchone()
            assert row['reference_image_ids'] == original_row['reference_image_ids']
            assert row['model_config_snapshot'] == original_row['model_config_snapshot']
        # The original failure remains available through GET /api/tasks but is superseded in derived state.
        batch_state = client.get(f"/api/batches/{batch['id']}").json()
        assert batch_state['status'] == 'completed'
        assert batch_state['total'] == 1
        assert batch_state['counts']['completed'] == 1
        assert batch_state['counts']['failed'] == 0
        second_retry = client.post(f"/api/tasks/{original['id']}/retry")
        assert second_retry.status_code == 409
        assert second_retry.json()['detail'] == {
            'code': 'task_already_retried',
            'message': '该任务已经重试过，请查看最新的任务',
        }
        assert client.delete(f"/api/projects/{project['id']}").status_code == 204


def test_cancel_batch_preserves_running_result(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'])
        assert generator.started.wait(2)
        result = client.post(f"/api/batches/{batch['id']}/cancel").json()
        assert result['status'] == 'running'
        assert result['counts']['cancelled'] == 5
        assert result['counts']['running'] == 1
        generator.release.set()
        final = wait_batch(client, batch['id'])
        assert final['counts']['completed'] == 1
        assert final['counts']['cancelled'] == 5
        assert len(generator.calls) == 1
        client.post(f"/api/batches/{batch['id']}/continue")
        assert len(batch_tasks(client, batch['id'])) == 6


def test_auth_failure_holds_remaining_and_continue_resumes(app_factory):
    generator = ScriptedGenerator([GeneratorError('auth_or_quota')])
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'])
        held = wait_batch(client, batch['id'], status='held')
        assert held['counts']['held'] == 5 and held['counts']['failed'] == 1
        assert len(generator.calls) == 1
        project_state = client.get(f"/api/projects/{project['id']}").json()
        assert project_state['has_pending_recovery'] is True
        assert project_state['active_task_count'] == 0
        original_failure = next(t for t in batch_tasks(client, batch['id']) if t['status'] == 'failed')
        assert original_failure['error_code'] == 'auth_or_quota'
        response = client.post(f"/api/batches/{batch['id']}/continue")
        assert response.status_code == 200
        done = wait_until(lambda: client.get(f"/api/batches/{batch['id']}").json(), lambda b: b['counts']['completed'] == 5)
        assert done['status'] == 'needs_attention'
        assert len(generator.calls) == 6
        assert original_failure == next(t for t in batch_tasks(client, batch['id']) if t['id'] == original_failure['id'])


def test_retrying_interrupted_task_clears_pending_recovery(app_factory):
    generator = ScriptedGenerator([GeneratorError('unknown_outcome')])
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        assert client.get(f"/api/projects/{project['id']}").json()['has_pending_recovery'] is True
        original = batch_tasks(client, batch['id'])[0]
        retry = client.post(f"/api/tasks/{original['id']}/retry")
        assert retry.status_code == 201
        wait_batch(client, batch['id'])
        state = client.get(f"/api/projects/{project['id']}").json()
        assert state['has_pending_recovery'] is False
        assert state['active_task_count'] == 0


def test_concurrent_manual_retries_only_create_one_task(app_factory):
    generator = ScriptedGenerator([GeneratorError('rejected')])
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        original = batch_tasks(client, batch['id'])[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post(f"/api/tasks/{original['id']}/retry"), range(2)))
        assert sorted(response.status_code for response in responses) == [201, 409]
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json()['detail']['code'] == 'task_already_retried'
        assert len(batch_tasks(client, batch['id'])) == 2


def test_held_tasks_can_be_cancelled_individually_and_by_batch(app_factory):
    generator = ScriptedGenerator([GeneratorError('auth_or_quota')])
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'])
        wait_batch(client, batch['id'], status='held')
        held_task = next(t for t in batch_tasks(client, batch['id']) if t['status'] == 'held')
        assert client.post(f"/api/tasks/{held_task['id']}/cancel").json()['status'] == 'cancelled'
        cancelled = client.post(f"/api/batches/{batch['id']}/cancel").json()
        assert cancelled['counts']['cancelled'] == 5
        assert cancelled['counts']['failed'] == 1
        assert cancelled['status'] == 'needs_attention'
        assert client.post(f"/api/tasks/{held_task['id']}/cancel").status_code == 409


def test_rate_limit_backs_off_globally_and_retains_waiting_state(app_factory):
    generator = ScriptedGenerator([GeneratorError('rate_limited', retry_after=.25)])
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'])
        limited = wait_until(lambda: batch_tasks(client, batch['id']), lambda tasks: any(t['error_code'] == 'rate_limited' for t in tasks))
        assert all(t['status'] == 'waiting' for t in limited)
        assert len(generator.calls) == 1
        assert client.get(f"/api/batches/{batch['id']}").json()['status'] == 'running'
        wait_batch(client, batch['id'])
        assert len(generator.calls) == 7
        assert generator.calls[1][2] - generator.calls[0][2] >= .23
        assert all(t['auto_retry_count'] == 0 for t in batch_tasks(client, batch['id']))


def test_unexpected_generator_error_is_logged_and_sanitized(app_factory, caplog):
    generator = ScriptedGenerator([RuntimeError('provider internal diagnostic')])
    with app_factory(generator=generator) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        task = batch_tasks(client, batch['id'])[0]
        assert task['error_code'] == 'generation_failed'
        assert task['error_message'] == '图片生成失败，请稍后重试'
        assert 'provider internal diagnostic' in caplog.text
        assert len(generator.calls) == 1


def test_regeneration_failure_retains_previous_version(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        first = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, first['id'])
        original = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        generator.outcomes.append(GeneratorError('rejected'))
        second = preview_batch(client, project['id'], request_id='second', style_ids=['korean_minimal'])
        wait_batch(client, second['id'], status='needs_attention')
        current = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert current['current_version'] == original['current_version']
        assert current['version_count'] == 1
        assert current['latest_task']['status'] == 'failed'
        assert client.get(f"/api/assets/{current['current_version']['asset_id']}").content == PNG


def test_storage_failure_holds_waiting_tasks_and_never_marks_complete(app_factory):
    with app_factory(concurrency=1) as (client, app):
        project = create_project(client)
        with patch('app.task_manager.save_image', side_effect=OSError(28, 'No space left on device')):
            batch = preview_batch(client, project['id'])
            held = wait_batch(client, batch['id'], status='held')
        assert held['counts']['failed'] == 1 and held['counts']['held'] == 5
        failure = next(t for t in batch_tasks(client, batch['id']) if t['status'] == 'failed')
        assert failure['error_code'] == 'storage_error'
        assert str(app.state.config.data_dir) in failure['error_message']
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM photo_versions').fetchone()[0] == 0
        client.post(f"/api/batches/{batch['id']}/continue")
        wait_until(lambda: client.get(f"/api/batches/{batch['id']}").json(), lambda b: b['counts']['completed'] == 5)


def test_version_transaction_rolls_back_on_insert_failure(app_factory):
    with app_factory(concurrency=1) as (client, app):
        project = create_project(client)
        with app.state.db.connect(write=True) as conn:
            conn.execute("CREATE TRIGGER reject_version BEFORE INSERT ON photo_versions BEGIN SELECT RAISE(ABORT, 'test version failure'); END")
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        with app.state.db.connect() as conn:
            # Two people, each stored as the untouched upload plus the prepared reference; no generated asset.
            assert conn.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0] == 4
            assert conn.execute('SELECT COUNT(*) FROM photo_versions').fetchone()[0] == 0
            task = conn.execute('SELECT * FROM generation_tasks').fetchone()
            assert task['status'] == 'failed' and task['output_asset_id'] is None
            assert conn.execute('SELECT current_version_id FROM photo_slots').fetchone()[0] is None


def test_startup_recovery_does_not_resubmit_and_cleans_temp(app_factory, tmp_path):
    data_dir = tmp_path / 'recovery'
    with app_factory(data_dir=data_dir) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'])
        wait_batch(client, batch['id'])
        with app.state.db.connect() as conn:
            original = [dict(row) for row in conn.execute('SELECT * FROM generation_tasks ORDER BY created_at')]
        db = app.state.db
    statuses = ['running', 'waiting', 'completed', 'failed', 'cancelled', 'interrupted']
    with db.connect(write=True) as conn:
        for task, status in zip(original, statuses):
            conn.execute('UPDATE generation_tasks SET status=? WHERE id=?', (status, task['id']))
            if status != 'completed':
                conn.execute('UPDATE photo_slots SET current_version_id=NULL WHERE id=?', (task['target_slot_id'],))
                conn.execute('UPDATE generation_tasks SET output_asset_id=NULL WHERE id=?', (task['id'],))
                conn.execute('DELETE FROM photo_versions WHERE generation_task_id=?', (task['id'],))
    temp = data_dir / 'temp'
    (temp / 'leftover.tmp').write_bytes(b'partial')
    (temp / 'old-directory').mkdir()
    (temp / 'old-directory' / 'leftover').write_bytes(b'partial')
    generator = ScriptedGenerator()
    with app_factory(generator=generator, data_dir=data_dir) as (client, app):
        tasks = {t['id']: t for t in batch_tasks(client, batch['id'])}
        assert tasks[original[0]['id']]['status'] == 'interrupted'
        assert tasks[original[0]['id']]['error_code'] == 'interrupted_by_restart'
        assert tasks[original[0]['id']]['error_message'] == '后端重启时这次请求还在进行，结果无法确认，可能已经扣费。重新生成可能产生额外费用'
        assert tasks[original[1]['id']]['status'] == 'held'
        for task, status in zip(original[2:], statuses[2:]):
            assert tasks[task['id']]['status'] == status
        assert list(temp.iterdir()) == []
        assert not generator.calls
        state = client.get(f"/api/projects/{project['id']}").json()
        assert state['has_pending_recovery'] is True and state['active_task_count'] == 0
        assert sum(slot['current_version'] is not None for slot in state['previews']) == 1
        held_batch = client.get(f"/api/batches/{batch['id']}").json()
        assert held_batch['status'] == 'held'
        client.post(f"/api/batches/{batch['id']}/continue")
        wait_until(lambda: batch_tasks(client, batch['id']), lambda rows: next(t for t in rows if t['id'] == original[1]['id'])['status'] == 'completed')
        assert len(generator.calls) == 1
        assert next(t for t in batch_tasks(client, batch['id']) if t['id'] == original[0]['id'])['status'] == 'interrupted'


def test_scheduler_respects_concurrency_and_wakes_promptly(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=2) as (client, app):
        project = create_project(client)
        started = time.monotonic()
        batch = preview_batch(client, project['id'])
        tasks = wait_until(lambda: batch_tasks(client, batch['id']), lambda rows: sum(t['status'] == 'running' for t in rows) == 2)
        assert time.monotonic() - started < .8
        assert sum(t['status'] == 'waiting' for t in tasks) == 4
        wait_until(lambda: len(generator.calls), lambda calls: calls == 2)
        time.sleep(.05)
        assert len(generator.calls) == 2
        generator.release.set()
        wait_batch(client, batch['id'])
        assert len(generator.calls) == 6


def test_concurrent_versions_have_unique_monotonic_numbers(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=2) as (client, app):
        project = create_project(client)
        first = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        assert generator.started.wait(2)
        second = preview_batch(client, project['id'], request_id='second', style_ids=['korean_minimal'])
        wait_until(lambda: len(generator.calls), lambda n: n == 2)
        generator.release.set()
        wait_batch(client, first['id'])
        wait_batch(client, second['id'])
        with app.state.db.connect() as conn:
            versions = conn.execute('SELECT version_number FROM photo_versions ORDER BY version_number').fetchall()
            assert [v[0] for v in versions] == [1, 2]
            assert conn.execute('SELECT COUNT(*) FROM photo_slots').fetchone()[0] == 1


def test_atomic_claim_never_returns_same_waiting_task_twice(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'])
        assert generator.started.wait(2)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=5) as executor:
            claimed = list(executor.map(lambda _: app.state.task_manager._claim(), range(5)))
        assert len({task['id'] for task in claimed}) == 5
        assert all(task is not None for task in claimed)
        assert app.state.task_manager._claim() is None
        # Test-only claims did not submit requests; return them to held for cleanup.
        with app.state.db.connect(write=True) as conn:
            for task in claimed:
                conn.execute("UPDATE generation_tasks SET status='held' WHERE id=?", (task['id'],))
        generator.release.set()
        wait_batch(client, batch['id'], status='held')


def test_file_is_fsynced_and_moved_before_database_completion(app_factory):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append('fsync')
        return real_fsync(fd)

    def replace(source, destination):
        events.append('replace')
        return real_replace(source, destination)

    real_commit = TaskManager._commit_result

    def commit(manager, task, asset_id, relative_path):
        assert asset_path(manager.config, relative_path).read_bytes() == PNG
        with manager.db.connect() as conn:
            assert conn.execute('SELECT status FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()[0] == 'running'
        events.append('commit')
        return real_commit(manager, task, asset_id, relative_path)

    with app_factory(concurrency=1) as (client, app):
        project = create_project(client)
        with patch('app.storage.os.fsync', side_effect=fsync), patch('app.storage.os.replace', side_effect=replace), patch.object(TaskManager, '_commit_result', new=commit):
            batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
            wait_batch(client, batch['id'])
        assert events == ['fsync', 'replace', 'fsync', 'commit']


def test_missing_reference_fails_without_provider_request(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        with app.state.db.connect() as conn:
            row = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (project['person_a']['asset_id'],)).fetchone()
        asset_path(app.state.config, row['file_path']).unlink()
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        assert batch_tasks(client, batch['id'])[0]['error_code'] == 'reference_missing'
        assert not generator.calls


def test_schema_includes_future_entities_and_does_not_store_derived_state(client_app):
    _, app = client_app
    expected = {'projects', 'image_assets', 'style_presets', 'photo_slots', 'photo_versions', 'generation_batches', 'generation_tasks', 'app_settings'}
    with app.state.db.connect() as conn:
        tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert expected <= tables
        for table in ('projects', 'generation_batches'):
            columns = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
            assert 'status' not in columns and 'stage' not in columns
        assert 'person_b_role' not in {r['name'] for r in conn.execute('PRAGMA table_info(projects)')}
        task_columns = {r['name'] for r in conn.execute('PRAGMA table_info(generation_tasks)')}
        assert {'style_id', 'pose_id', 'reference_image_ids', 'auto_retry_count', 'retry_of_task_id', 'model_config_snapshot', 'provider_task_id', 'output_asset_id'} <= task_columns
        assert conn.execute('SELECT COUNT(*) FROM style_presets').fetchone()[0] == len(STYLES)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='task_retry_once'").fetchone() is not None
        conn.execute('DROP INDEX task_retry_once')
    # Schema initialization also migrates an already-created database file.
    app.state.db.initialize()
    with app.state.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='task_retry_once'").fetchone() is not None


def test_config_validation_and_unsupported_provider(tmp_path):
    from app.main import create_app

    with pytest.raises(ValueError):
        Config(tmp_path, 0, 'mock', 0)
    with pytest.raises(ValueError):
        Config(tmp_path, 1, 'mock', -1)
    with pytest.raises(ValueError):
        create_app(Config(tmp_path, 1, 'unknown', 0))
