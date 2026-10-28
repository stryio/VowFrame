"""Per-task model selection and persistent default settings."""

import json
import logging

import pytest

from app.config import load_config
from app.generator import GeneratorError
from app.models import MODEL_REGISTRY
from conftest import BlockingGenerator, ScriptedGenerator, create_project, preview_batch, wait_batch, wait_until


def test_registry_and_models_endpoint(client_app):
    client, _ = client_app
    models = client.get('/api/models').json()
    assert [model['id'] for model in models] == [
        'gpt-image-2', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst', 'gemini-nano-banana-2.1',
    ]
    assert [model['family'] for model in models] == ['openai', 'openai', 'openai', 'gemini']
    assert [model['max_reference_images'] for model in models] == [4, 10, 10, 6]
    assert models == [model.__dict__ for model in MODEL_REGISTRY]


def test_settings_patch_validation_and_persistence_across_restart(app_factory, tmp_path):
    data_dir = tmp_path / 'persistent-data'
    with app_factory(data_dir=data_dir) as (client, _):
        assert client.get('/api/settings').json() == {'default_model': 'gpt-image-2', 'batch_reference_mode': 'prompt_only', 'director_model': None, 'portrait_ratio': '3:4', 'auto_face_swap': False}
        assert client.patch('/api/settings', json={'default_model': 'gpt-image-2.5-flare', 'batch_reference_mode': 'with_preview', 'portrait_ratio': '9:16'}).json() == {
            'default_model': 'gpt-image-2.5-flare', 'batch_reference_mode': 'with_preview', 'director_model': None, 'portrait_ratio': '9:16',
            'auto_face_swap': False,
        }
        invalid = client.patch('/api/settings', json={'default_model': 'unknown-model'})
        assert invalid.status_code == 422
        assert invalid.json()['detail'] == {'code': 'invalid_model', 'message': '请选择有效的生成模型'}
        landscape = client.patch('/api/settings', json={'portrait_ratio': '16:9'})
        assert landscape.status_code == 422
        assert landscape.json()['detail']['code'] == 'invalid_portrait_ratio'

    with app_factory(data_dir=data_dir) as (client, _):
        assert client.get('/api/settings').json() == {'default_model': 'gpt-image-2.5-flare', 'batch_reference_mode': 'with_preview', 'director_model': None, 'portrait_ratio': '9:16', 'auto_face_swap': False}
        assert client.get('/api/health').json()['model'] == 'gpt-image-2.5-flare'


def test_invalid_local_default_logs_warning_and_uses_supported_fallback(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'local.json'
    path.write_text(json.dumps({'model': 'not-registered'}))
    monkeypatch.setenv('VOWFRAME_LOCAL_CONFIG', str(path))
    with caplog.at_level(logging.WARNING, logger='app.config'):
        config = load_config()
    assert config.model == 'gpt-image-2'
    assert '本地配置中的模型不受支持' in caplog.text


def test_preview_model_is_returned_in_snapshot_task_and_version(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'], model='gemini-nano-banana-2.1')
        wait_batch(client, batch['id'])
        task = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
        detail = client.get(f"/api/projects/{project['id']}").json()
        version = detail['previews'][0]['current_version']
        with app.state.db.connect() as conn:
            snapshot = json.loads(conn.execute(
                'SELECT model_config_snapshot FROM generation_tasks WHERE id=?', (task['id'],),
            ).fetchone()[0])
        assert snapshot['model'] == 'gemini-nano-banana-2.1'
        assert task['model'] == version['model'] == 'gemini-nano-banana-2.1'
        assert generator.models == ['gemini-nano-banana-2.1']


def test_omitted_preview_model_uses_persisted_default(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator) as (client, app):
        client.patch('/api/settings', json={'default_model': 'gpt-image-2.5-sunburst'})
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        task = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
        with app.state.db.connect() as conn:
            snapshot = json.loads(conn.execute(
                'SELECT model_config_snapshot FROM generation_tasks WHERE id=?', (task['id'],),
            ).fetchone()[0])
        assert snapshot['model'] == task['model'] == 'gpt-image-2.5-sunburst'
        assert generator.models == ['gpt-image-2.5-sunburst']


def test_default_change_does_not_change_waiting_task_models(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, _):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal', 'french_vintage'], model='gpt-image-2.5-flare')
        assert generator.started.wait(2)
        client.patch('/api/settings', json={'default_model': 'gemini-nano-banana-2.1'})
        generator.release.set()
        wait_batch(client, batch['id'])
        tasks = client.get('/api/tasks', params={'batch_id': batch['id']}).json()
        assert [task['model'] for task in tasks] == ['gpt-image-2.5-flare'] * 2
        assert generator.models == ['gpt-image-2.5-flare'] * 2


def test_invalid_preview_model_is_rejected(app_factory):
    with app_factory() as (client, _):
        project = create_project(client)
        response = client.post(f"/api/projects/{project['id']}/previews", json={
            'client_request_id': 'unknown-model', 'style_ids': ['korean_minimal'], 'model': 'unknown-model',
        })
        assert response.status_code == 422
        assert response.json()['detail']['code'] == 'invalid_model'


def test_legacy_snapshot_uses_current_default_and_exposes_null_model(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        assert generator.started.wait(2)
        second = preview_batch(client, project['id'], request_id='legacy', style_ids=['french_vintage'])
        with app.state.db.connect(write=True) as conn:
            row = conn.execute('SELECT id,model_config_snapshot FROM generation_tasks WHERE batch_id=?', (second['id'],)).fetchone()
            snapshot = json.loads(row['model_config_snapshot'])
            snapshot.pop('model')
            conn.execute('UPDATE generation_tasks SET model_config_snapshot=? WHERE id=?', (json.dumps(snapshot), row['id']))
        client.patch('/api/settings', json={'default_model': 'gemini-nano-banana-2.1'})
        generator.release.set()
        wait_batch(client, batch['id'])
        wait_batch(client, second['id'])
        task = client.get('/api/tasks', params={'batch_id': second['id']}).json()[0]
        detail = client.get(f"/api/projects/{project['id']}").json()
        legacy_version = next(slot['current_version'] for slot in detail['previews'] if slot['style_id'] == 'french_vintage')
        assert task['model'] is None
        assert legacy_version['model'] is None
        assert generator.models == ['gpt-image-2', 'gemini-nano-banana-2.1']


@pytest.mark.parametrize('model', ['gpt-image-2', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst', 'gemini-nano-banana-2.1'])
def test_every_registry_model_accepts_two_reference_images(app_factory, model):
    with app_factory() as (client, _):
        project = create_project(client)
        response = client.post(f"/api/projects/{project['id']}/previews", json={
            'client_request_id': 'model-' + model, 'style_ids': ['korean_minimal'], 'model': model,
        })
        assert response.status_code == 201


def test_retry_keeps_original_model_after_default_changes(app_factory):
    generator = ScriptedGenerator([GeneratorError('rejected')])
    with app_factory(generator=generator, concurrency=1) as (client, _):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'], model='gpt-image-2.5-flare')
        wait_batch(client, batch['id'], status='needs_attention')
        original = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
        client.patch('/api/settings', json={'default_model': 'gemini-nano-banana-2.1'})
        retry = client.post(f"/api/tasks/{original['id']}/retry").json()
        wait_until(lambda: client.get('/api/tasks', params={'batch_id': batch['id']}).json(),
                   lambda tasks: next(task for task in tasks if task['id'] == retry['id'])['status'] == 'completed')
        assert retry['model'] == original['model'] == 'gpt-image-2.5-flare'
        assert generator.models == ['gpt-image-2.5-flare', 'gpt-image-2.5-flare']
