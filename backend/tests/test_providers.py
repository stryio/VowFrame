"""Image services (V7.11): 88API or PPTOKEN, switched in Settings. Models both offer go to the selected one; a model
only one offers always goes there; PPTOKEN can be selected only when its key is configured."""

import base64
import logging
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api88 import Api88Generator
from app.config import Config, load_config
from app.generator import GeneratorError, OutcomeKind, ProviderGenerator
from app.main import create_app
from app.photo_prep import PassthroughPhotoPreparer
from conftest import PNG, ScriptedGenerator, create_project, ensure_looks

KEY_88, KEY_PP = 'sk-88api-test-key', 'sk-pptoken-test-key'


def image_response():
    return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(PNG).decode()}]})


def routed(tmp_path, selected):
    """A router over 88API and PPTOKEN clients whose requests are recorded; selected() is the Settings choice."""
    hosts = []

    def handler(request):
        hosts.append((request.url.host, request.headers['Authorization']))
        return image_response()

    config = Config(tmp_path / 'data', 1, '88api', 0, api_key=KEY_88)
    transport = httpx.MockTransport(handler)
    generators = {
        '88api': Api88Generator(config, transport=transport),
        'pptoken': Api88Generator(replace(config, base_url='https://api.pptoken.cc', api_key=KEY_PP), transport=transport,
                                  provider='PPTOKEN'),
    }
    return ProviderGenerator(generators, selected), hosts


def test_each_model_goes_to_the_service_that_offers_it(tmp_path):
    image = tmp_path / 'photo.png'
    image.write_bytes(PNG)
    choice = ['pptoken']
    router, hosts = routed(tmp_path, lambda: choice[0])
    router.generate_image([image], 'p', 'gpt-image-2', '3:4')        # both offer it: the selected one
    router.generate_image([image], 'p', 'gpt-image-2.5', '3:4')      # only PPTOKEN
    router.generate_image([image], 'p', 'gpt-image-2.5-sunburst', '3:4')  # only 88API, e.g. an older task's replay
    choice[0] = '88api'
    router.generate_image([image], 'p', 'gpt-image-2', '3:4')
    assert hosts == [('api.pptoken.cc', f'Bearer {KEY_PP}'), ('api.pptoken.cc', f'Bearer {KEY_PP}'),
                     ('api.88api.ai', f'Bearer {KEY_88}'), ('api.88api.ai', f'Bearer {KEY_88}')]


def test_a_service_without_a_key_is_never_sent_anything(tmp_path):
    image = tmp_path / 'photo.png'
    image.write_bytes(PNG)
    router, hosts = routed(tmp_path, lambda: 'pptoken')
    del router.generators['pptoken']
    with pytest.raises(GeneratorError) as raised:
        router.generate_image([image], 'p', 'gpt-image-2', '3:4')
    assert raised.value.kind == OutcomeKind.NOT_SENT and 'PPTOKEN' in raised.value.message and hosts == []


def test_pptoken_errors_are_logged_under_its_name_without_its_key(tmp_path, caplog):
    image = tmp_path / 'photo.png'
    image.write_bytes(PNG)
    config = Config(tmp_path / 'data', 1, '88api', 0, api_key=KEY_PP, base_url='https://api.pptoken.cc')
    client = Api88Generator(config, transport=httpx.MockTransport(lambda request: httpx.Response(400, json={'error': f'bad size {KEY_PP}'})),
                            provider='PPTOKEN')
    with caplog.at_level(logging.WARNING), pytest.raises(GeneratorError):
        client.generate_image([image], 'p', 'gpt-image-2', '3:4')
    assert 'PPTOKEN HTTP 400' in caplog.text and KEY_PP not in caplog.text


def test_the_local_config_reads_the_pptoken_key_and_builds_its_client(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(f'{{"generator": "88api", "api_key": "{KEY_88}", "pptoken_api_key": "{KEY_PP}"}}')
    monkeypatch.setenv('VOWFRAME_LOCAL_CONFIG', str(path))
    monkeypatch.delenv('VOWFRAME_GENERATOR', raising=False)
    config = load_config()
    assert (config.pptoken_api_key, config.pptoken_base_url) == (KEY_PP, 'https://api.pptoken.cc')
    assert KEY_PP not in repr(config)
    generator = create_app(replace(config, data_dir=tmp_path / 'data')).state.generator
    assert set(generator.generators) == {'88api', 'pptoken'}
    assert generator.generators['pptoken'].provider == 'PPTOKEN'


def test_settings_switch_the_service_and_keep_the_default_model_one_it_offers(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, _app):
        settings = client.get('/api/settings').json()
        assert settings['image_provider'] == '88api'
        assert [(item['id'], item['available']) for item in settings['image_providers']] == [('88api', True), ('pptoken', True)]
        assert client.patch('/api/settings', json={'default_model': 'gpt-image-2.5-sunburst'}).status_code == 200
        assert client.patch('/api/settings', json={'default_model': 'gpt-image-2.5'}).status_code == 422  # PPTOKEN only
        # PPTOKEN has no Sunburst: the default becomes its first model.
        saved = client.patch('/api/settings', json={'image_provider': 'pptoken'}).json()
        assert (saved['image_provider'], saved['default_model']) == ('pptoken', 'gpt-image-2')
        assert [model['id'] for model in client.get('/api/models').json()] == ['gpt-image-2', 'gpt-image-2.5']
        assert client.patch('/api/settings', json={'image_provider': '88api', 'default_model': 'gpt-image-2.5'}).status_code == 422
        assert client.patch('/api/settings', json={'image_provider': 'elsewhere'}).status_code == 422
        # A request naming a model the service does not offer is refused.
        project = create_project(client)
        ensure_looks(client, project['id'], ['korean_minimal'])
        response = client.post(f"/api/projects/{project['id']}/previews", json={
            'client_request_id': 'sunburst', 'style_ids': ['korean_minimal'], 'model': 'gpt-image-2.5-sunburst'})
        assert response.status_code == 422 and response.json()['detail']['code'] == 'invalid_model'


def test_pptoken_cannot_be_selected_without_its_key(tmp_path):
    config = Config(tmp_path / 'data', 1, '88api', 0, api_key=KEY_88)
    app = create_app(config, ScriptedGenerator(), photo_preparer=PassthroughPhotoPreparer(), stylist=ScriptedGenerator())
    with TestClient(app, backend_options={'use_uvloop': True}) as client:
        pptoken = client.get('/api/settings').json()['image_providers'][1]
        assert not pptoken['available'] and 'pptoken_api_key' in pptoken['reason']
        response = client.patch('/api/settings', json={'image_provider': 'pptoken'})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'provider_unavailable'
        assert client.get('/api/settings').json()['image_provider'] == '88api'
