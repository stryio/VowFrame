"""火山方舟 Seedream (2026-11-13): the request Ark gets, its errors, routing and Settings, without network access."""

import base64
import io
import json
import logging
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api88 import size_problem
from app.ark import ArkGenerator
from app.config import Config, load_config
from app.generator import GeneratorError, OutcomeKind
from app.main import create_app
from app.photo_prep import PassthroughPhotoPreparer
from conftest import PNG, ScriptedGenerator

KEY_88, KEY_ARK = 'sk-88api-test-key', 'ark-unit-test-secret-never-log'
MODEL = 'doubao-seedream-5-5-261028'
ARK_URL = 'https://ark.cn-beijing.volces.com/api/v3'


def png(width, height):
    output = io.BytesIO()
    Image.new('RGB', (width, height), 'white').save(output, 'PNG')
    return output.getvalue()


def ark(tmp_path, handler):
    config = Config(tmp_path / 'data', 1, '88api', 0, api_key=KEY_ARK, base_url=ARK_URL)
    return ArkGenerator(config, transport=httpx.MockTransport(handler))


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / 'bride.png'
    path.write_bytes(PNG)
    return path


def test_the_request_is_ark_json_with_data_uri_references_and_exact_pixels(tmp_path, photo):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(PNG).decode(), 'size': '1536x2048'}]})

    assert ark(tmp_path, handler).generate_image([photo, photo], '第一张图是新娘', MODEL, '3:4', '2K') == PNG
    request = seen[0]
    assert str(request.url) == ARK_URL + '/images/generations'
    assert request.headers['Authorization'] == f'Bearer {KEY_ARK}'
    body = json.loads(request.content)
    assert {key: body[key] for key in ('model', 'prompt', 'size', 'response_format', 'watermark')} == {
        'model': MODEL, 'prompt': '第一张图是新娘', 'size': '1536x2048', 'response_format': 'b64_json', 'watermark': False}
    assert len(body['image']) == 2 and all(uri.startswith('data:image/png;base64,') for uri in body['image'])


def test_text_only_requests_send_no_image_field(tmp_path):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(PNG).decode()}]})

    ark(tmp_path, handler).generate_image([], '空景', MODEL, '1:1', '1K')
    assert 'image' not in bodies[0] and bodies[0]['size'] == '1024x1024'


def test_4k_is_refused_before_anything_is_sent(tmp_path, photo):
    def handler(request):
        raise AssertionError('nothing may be sent')

    with pytest.raises(GeneratorError) as raised:
        ark(tmp_path, handler).generate_image([photo], 'p', MODEL, '3:4', '4K')
    assert raised.value.kind == OutcomeKind.NOT_SENT and '2K' in raised.value.message


@pytest.mark.parametrize('status, kind', [(401, OutcomeKind.AUTH_OR_QUOTA), (429, OutcomeKind.RATE_LIMITED),
                                          (400, OutcomeKind.REJECTED), (504, OutcomeKind.UNKNOWN_OUTCOME)])
def test_errors_are_classified_like_the_other_services_and_never_log_the_key(tmp_path, photo, caplog, status, kind):
    def handler(request):
        return httpx.Response(status, json={'error': {'code': 'X', 'message': f'bad key {KEY_ARK}'}})

    with caplog.at_level(logging.WARNING), pytest.raises(GeneratorError) as raised:
        ark(tmp_path, handler).generate_image([photo], 'p', MODEL, '3:4', '2K')
    assert raised.value.kind == kind
    assert KEY_ARK not in caplog.text and '火山方舟 HTTP' in caplog.text


def test_a_refused_picture_is_a_rejection(tmp_path, photo):
    def handler(request):
        return httpx.Response(200, json={'data': [{'error': {'code': 'OutputImageSensitiveContentDetected'}}]})

    with pytest.raises(GeneratorError) as raised:
        ark(tmp_path, handler).generate_image([photo], 'p', MODEL, '3:4', '2K')
    assert raised.value.kind == OutcomeKind.REJECTED


def test_the_returned_size_is_checked(tmp_path):
    assert size_problem(png(1536, 2048), MODEL, '3:4', '2K') is None
    assert size_problem(png(1024, 1024), MODEL, '3:4', '2K') is not None


def test_the_local_config_builds_the_ark_client_and_settings_offer_it(tmp_path, monkeypatch):
    path = tmp_path / 'config.local.json'
    path.write_text(json.dumps({'generator': '88api', 'api_key': KEY_88, 'ark_api_key': KEY_ARK}))
    monkeypatch.setenv('VOWFRAME_LOCAL_CONFIG', str(path))
    monkeypatch.delenv('VOWFRAME_GENERATOR', raising=False)
    config = replace(load_config(), data_dir=tmp_path / 'data')
    assert (config.ark_api_key, config.ark_base_url) == (KEY_ARK, ARK_URL)
    assert KEY_ARK not in repr(config)
    app = create_app(config, photo_preparer=PassthroughPhotoPreparer(), stylist=ScriptedGenerator())
    assert isinstance(app.state.generator.generators['ark'], ArkGenerator)
    with TestClient(app, backend_options={'use_uvloop': True}) as client:
        settings = client.get('/api/settings').json()
        assert [item['id'] for item in settings['director_providers']] == ['88api', 'pptoken']
        saved = client.patch('/api/settings', json={'image_provider': 'ark'}).json()
        assert (saved['image_provider'], saved['default_model']) == ('ark', MODEL)
        assert [model['id'] for model in client.get('/api/models').json()] == [MODEL]
        assert client.patch('/api/settings', json={'director_provider': 'ark'}).status_code == 422


def test_ark_cannot_be_selected_without_its_key(tmp_path):
    config = Config(tmp_path / 'data', 1, '88api', 0, api_key=KEY_88)
    app = create_app(config, ScriptedGenerator(), photo_preparer=PassthroughPhotoPreparer(), stylist=ScriptedGenerator())
    with TestClient(app, backend_options={'use_uvloop': True}) as client:
        entry = client.get('/api/settings').json()['image_providers'][2]
        assert not entry['available'] and 'ark_api_key' in entry['reason']
        response = client.patch('/api/settings', json={'image_provider': 'ark'})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'provider_unavailable'
