"""88API contract, billing safety and secret isolation without network access."""

import ast
import base64
import io
import json
import logging
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.api88 import (
    AUTH_MESSAGE, NO_IMAGE_MESSAGE, OPENAI_SIZES, REJECTED_MESSAGE,
    SERVICE_MESSAGE, Api88Generator, retry_after_seconds,
)
from app.config import Config, load_config
from app.generator import GeneratorError, MockGenerator, OutcomeKind
from app.main import create_app
from app.storage import outgoing_image
from conftest import PNG, create_project, preview_batch, wait_batch

TEST_KEY = 'sk-unit-test-secret-never-log'
PROMPT = '第一张图是新娘，第二张图是新郎'


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path / 'data', 1, '88api', 0, api_key=TEST_KEY)


@pytest.fixture
def images(tmp_path):
    bride, groom = tmp_path / 'bride.png', tmp_path / 'groom.jpg'
    bride.write_bytes(PNG)
    Image.new('RGB', (4, 6), 'navy').save(groom, 'JPEG')
    return [bride, groom]


def sent(path):
    """Bytes the generator should upload for a reference photo."""
    return outgoing_image(path)[1]


def image_response():
    return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(PNG).decode()}]})


def make_generator(config, handler):
    return Api88Generator(config, transport=httpx.MockTransport(handler))


def multipart_parts(request):
    message = BytesParser(policy=default).parsebytes(
        b'Content-Type: ' + request.headers['Content-Type'].encode() + b'\r\n\r\n' + request.content)
    return [(part.get_param('name', header='Content-Disposition'), part.get_filename(),
             part.get_content_type(), part.get_payload(decode=True)) for part in message.iter_parts()]


@pytest.mark.parametrize('model', ['gpt-image-2', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst'])
def test_openai_multipart_and_b64_response(config, images, model):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == 'POST'
        assert str(request.url) == 'https://api.88api.ai/v1/images/edits'
        assert request.headers['Authorization'] == f'Bearer {TEST_KEY}'
        assert request.extensions['timeout'] == dict.fromkeys(('connect', 'read', 'write', 'pool'), 600)
        parts = multipart_parts(request)
        fields = {name: value.decode() for name, filename, _, value in parts if filename is None}
        assert fields == {'model': model, 'prompt': PROMPT, 'size': '1536x2048', 'n': '1'}
        assert [part for part in parts if part[1] is not None] == [
            ('image[]', 'bride.png', 'image/png', sent(images[0])),
            ('image[]', 'groom.jpg', 'image/jpeg', sent(images[1])),
        ]
        return image_response()

    generator = make_generator(replace(config, model=model, base_url=config.base_url + '/'), handler)
    assert generator.generate_image(images, PROMPT, model, '3:4') == PNG
    assert len(calls) == 1


@pytest.mark.parametrize('aspect_ratio,sizes', [('3:4', OPENAI_SIZES['3:4'])])
@pytest.mark.parametrize('resolution', ['1K', '2K', '4K'])
def test_openai_dimensions_match_reference_script(config, images, aspect_ratio, sizes, resolution):
    def handler(request):
        fields = {name: value.decode() for name, filename, _, value in multipart_parts(request) if filename is None}
        assert fields['size'] == sizes[resolution]
        return image_response()

    generator = make_generator(replace(config, aspect_ratio=aspect_ratio, resolution=resolution), handler)
    assert generator.generate_image(images, PROMPT, 'gpt-image-2', '3:4') == PNG


@pytest.mark.parametrize('model', ['gemini-nano-banana-2.1'])
def test_gemini_json_and_inline_markdown_response(config, images, model):
    def handler(request):
        assert request.method == 'POST'
        assert str(request.url) == 'https://api.88api.ai/v1/chat/completions'
        assert request.headers['Authorization'] == f'Bearer {TEST_KEY}'
        assert json.loads(request.content) == {
            'model': model,
            'messages': [{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{base64.b64encode(sent(images[0])).decode()}'}},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{base64.b64encode(sent(images[1])).decode()}'}},
                {'type': 'text', 'text': PROMPT},
            ]}],
            'extra_body': {'google': {'image_config': {'aspect_ratio': '3:4', 'image_size': '2K'}}},
        }
        encoded = base64.b64encode(PNG).decode()
        # Newlines in inline base64 and a later URL match the script's parsing.
        return httpx.Response(200, json={'choices': [{'message': {'content':
            f'完成 ![图片](data:image/png;base64,{encoded[:30]}\n{encoded[30:]}) ![备用](https://images.example/unused.png)'}}]})

    assert make_generator(replace(config, model=model), handler).generate_image(images, PROMPT, model, '3:4') == PNG


def test_single_adapter_routes_each_model_by_registry_family(config, images):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == '/v1/images/edits':
            fields = {name: value.decode() for name, filename, _, value in multipart_parts(request) if filename is None}
            assert fields['model'] == 'gpt-image-2.5-flare'
            return image_response()
        assert request.url.path == '/v1/chat/completions'
        assert json.loads(request.content)['model'] == 'gemini-nano-banana-2.1'
        encoded = base64.b64encode(PNG).decode()
        return httpx.Response(200, json={'choices': [{'message': {'content': f'![image](data:image/png;base64,{encoded})'}}]})

    generator = make_generator(config, handler)
    assert generator.generate_image(images, PROMPT, 'gpt-image-2.5-flare', '3:4') == PNG
    assert generator.generate_image(images, PROMPT, 'gemini-nano-banana-2.1', '3:4') == PNG
    assert [request.url.path for request in calls] == ['/v1/images/edits', '/v1/chat/completions']


def test_adapter_rejects_unknown_model_per_call(config, images):
    generator = make_generator(config, lambda request: pytest.fail('Unknown models must be rejected before transport'))
    with pytest.raises(ValueError, match='不支持此模型'):
        generator.generate_image(images, PROMPT, 'az-gpt-image-2', '3:4')


@pytest.mark.parametrize('model', ['gpt-image-2', 'gemini-nano-banana-2.1'])
def test_url_response_download_without_authorization(config, images, model):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == 'POST':
            url = 'https://images.example/result.png'
            payload = {'choices': [{'message': {'content': f'![图片]({url})'}}]} if model.startswith('gemini') else {'data': [{'url': url}]}
            return httpx.Response(200, json=payload)
        assert 'Authorization' not in request.headers
        if request.url.path == '/result.png':
            return httpx.Response(302, headers={'Location': 'https://cdn.example/final.png'})
        assert str(request.url) == 'https://cdn.example/final.png'
        return httpx.Response(200, content=PNG)

    assert make_generator(replace(config, model=model), handler).generate_image(images, PROMPT, model, '3:4') == PNG
    assert [request.method for request in calls] == ['POST', 'GET', 'GET']


@pytest.mark.parametrize('exception,kind', [
    (httpx.ConnectError, OutcomeKind.NOT_SENT),
    (httpx.ConnectTimeout, OutcomeKind.NOT_SENT),
    (socket.gaierror, OutcomeKind.NOT_SENT),
    (httpx.ReadTimeout, OutcomeKind.UNKNOWN_OUTCOME),
    (httpx.RemoteProtocolError, OutcomeKind.UNKNOWN_OUTCOME),
    (httpx.ReadError, OutcomeKind.UNKNOWN_OUTCOME),
    (httpx.WriteError, OutcomeKind.UNKNOWN_OUTCOME),
    (httpx.WriteTimeout, OutcomeKind.UNKNOWN_OUTCOME),
    (httpx.PoolTimeout, OutcomeKind.UNKNOWN_OUTCOME),
    (httpx.LocalProtocolError, OutcomeKind.UNKNOWN_OUTCOME),
    (httpx.DecodingError, OutcomeKind.UNKNOWN_OUTCOME),
    (httpx.StreamError, OutcomeKind.UNKNOWN_OUTCOME),
    (httpx.InvalidURL, OutcomeKind.UNKNOWN_OUTCOME),
    (OSError, OutcomeKind.UNKNOWN_OUTCOME),
])
def test_transport_failure_mapping_and_secret_redaction(config, images, caplog, exception, kind):
    def handler(request):
        raise exception(f'provider echoed {TEST_KEY}')

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GeneratorError) as raised:
            make_generator(config, handler).generate_image(images, PROMPT, 'gpt-image-2', '3:4')
    assert raised.value.kind == kind
    if kind == OutcomeKind.UNKNOWN_OUTCOME:
        assert raised.value.error_code == 'unknown_outcome'
        assert raised.value.message == '模型服务连接中断，结果无法确认，可能已经扣费。重新生成可能产生额外费用'
    assert TEST_KEY not in str(raised.value)
    assert TEST_KEY not in caplog.text
    assert all(TEST_KEY not in repr(record.__dict__) for record in caplog.records)


def test_dns_failure_is_not_sent(config, images):
    def handler(request):
        try:
            raise socket.gaierror('DNS lookup failed')
        except socket.gaierror as error:
            raise httpx.ConnectError('DNS lookup failed', request=request) from error

    with pytest.raises(GeneratorError) as raised:
        make_generator(config, handler).generate_image(images, PROMPT, 'gpt-image-2', '3:4')
    assert raised.value.kind == OutcomeKind.NOT_SENT


@pytest.mark.parametrize('status,detail,kind,message', [
    (401, 'invalid api key', OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE),
    (403, 'forbidden', OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE),
    (402, 'payment required', OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE),
    (400, 'quota exceeded', OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE),
    (422, 'account BALANCE too low', OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE),
    (500, 'insufficient funds', OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE),
    (502, 'quota exceeded', OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE),
    (504, '账户余额不足', OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE),
    (400, '账户余额不足', OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE),
    (400, 'content moderation rejected', OutcomeKind.REJECTED, REJECTED_MESSAGE),
    (422, 'invalid image parameters', OutcomeKind.REJECTED, REJECTED_MESSAGE),
    (500, 'internal server error', OutcomeKind.REJECTED, SERVICE_MESSAGE),
    (502, 'bad gateway', OutcomeKind.UNKNOWN_OUTCOME, '模型服务响应超时，结果无法确认，可能已经扣费。重新生成可能产生额外费用'),
    (503, 'service unavailable', OutcomeKind.REJECTED, SERVICE_MESSAGE),
    (504, 'gateway timeout', OutcomeKind.UNKNOWN_OUTCOME, '模型服务响应超时，结果无法确认，可能已经扣费。重新生成可能产生额外费用'),
])
def test_http_error_mapping_and_provider_details_only_in_redacted_logs(config, images, caplog, status, detail, kind, message):
    def handler(request):
        return httpx.Response(status, json={'error': {'message': f'{detail}; credential={TEST_KEY}'}})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GeneratorError) as raised:
            make_generator(config, handler).generate_image(images, PROMPT, 'gpt-image-2', '3:4')
    assert raised.value.kind == kind
    assert raised.value.message == message
    assert 'credential=' not in raised.value.message
    assert detail in caplog.text
    assert TEST_KEY not in caplog.text
    assert all(TEST_KEY not in repr(record.__dict__) for record in caplog.records)


@pytest.mark.parametrize('header,expected', [('12', 12), ('1.5', 1.5), ('0', 0), (None, None), ('invalid', None), ('nan', None), ('inf', None), ('-1', None)])
def test_rate_limit_retry_after(config, images, header, expected):
    def handler(request):
        return httpx.Response(429, headers={} if header is None else {'Retry-After': header}, json={'error': 'quota rate limit'})

    with pytest.raises(GeneratorError) as raised:
        make_generator(config, handler).generate_image(images, PROMPT, 'gpt-image-2', '3:4')
    assert raised.value.kind == OutcomeKind.RATE_LIMITED
    assert raised.value.retry_after == expected


def test_retry_after_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert 58 <= retry_after_seconds(format_datetime(future, usegmt=True)) <= 60


def test_dimensions_stay_identical_to_reference_script():
    reference = Path(__file__).resolve().parents[2] / '.claude' / 'skills' / 'image-gen' / 'scripts' / 'gen_image.py'
    tree = ast.parse(reference.read_text(encoding='utf-8'))
    sizes = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == 'OPENAI_SIZES' for target in node.targets))
    assert OPENAI_SIZES == ast.literal_eval(sizes)


@pytest.mark.parametrize('model,payload', [
    ('gpt-image-2', {}), ('gpt-image-2', {'data': []}),
    ('gpt-image-2', {'data': [{}]}), ('gpt-image-2', {'data': [{'b64_json': '%%%'}]}),
    ('gpt-image-2', {'data': [{'b64_json': 'a'}]}), ('gpt-image-2', []),
    ('gpt-image-2', {'data': [{'data_uri': 'invalid'}]}),
    ('gemini-nano-banana-2.1', {}), ('gemini-nano-banana-2.1', {'choices': []}),
    ('gemini-nano-banana-2.1', {'choices': [{'message': {'content': '没有生成图片'}}]}),
    ('gemini-nano-banana-2.1', {'choices': [{'message': {'content': [{'text': 'unexpected'}]}}]}),
])
def test_success_without_image_is_rejected(config, images, model, payload):
    with pytest.raises(GeneratorError) as raised:
        make_generator(replace(config, model=model), lambda request: httpx.Response(200, json=payload)).generate_image(images, PROMPT, model, '3:4')
    assert raised.value.kind == OutcomeKind.REJECTED
    assert raised.value.message == NO_IMAGE_MESSAGE


def test_invalid_json_is_rejected(config, images):
    with pytest.raises(GeneratorError) as raised:
        make_generator(config, lambda request: httpx.Response(200, text='not JSON')).generate_image(images, PROMPT, 'gpt-image-2', '3:4')
    assert raised.value.kind == OutcomeKind.REJECTED
    assert raised.value.message == NO_IMAGE_MESSAGE


def test_success_response_with_quota_error(config, images, caplog):
    def handler(request):
        return httpx.Response(200, json={'error': f'insufficient balance {TEST_KEY}'})

    with pytest.raises(GeneratorError) as raised:
        make_generator(config, handler).generate_image(images, PROMPT, 'gpt-image-2', '3:4')
    assert raised.value.kind == OutcomeKind.AUTH_OR_QUOTA
    assert TEST_KEY not in caplog.text


def test_escaped_chinese_quota_error_is_decoded(config, images):
    def handler(request):
        return httpx.Response(400, text=json.dumps({'error': '账户余额不足'}, ensure_ascii=True))

    with pytest.raises(GeneratorError) as raised:
        make_generator(config, handler).generate_image(images, PROMPT, 'gpt-image-2', '3:4')
    assert raised.value.kind == OutcomeKind.AUTH_OR_QUOTA


def test_download_redirect_cannot_leak_key_in_url_or_logs(config, images, caplog):
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(200, json={'data': [{'url': 'https://images.example/result.png'}]})
        return httpx.Response(302, headers={'Location': f'https://cdn.example/result.png?key={TEST_KEY}'})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GeneratorError) as raised:
            make_generator(config, handler).generate_image(images, PROMPT, 'gpt-image-2', '3:4')
    assert raised.value.kind == OutcomeKind.UNKNOWN_OUTCOME
    assert raised.value.error_code == 'download_failed'
    assert raised.value.message == '图片已生成但下载失败，可能已经扣费。重新生成可能产生额外费用'
    assert calls == ['POST', 'GET']


def test_read_error_file_log_is_redacted_and_create_app_does_not_duplicate_handlers(config, tmp_path, caplog):
    def handler(request):
        raise httpx.ReadError(f'provider echoed {TEST_KEY}', request=request)

    app = create_app(config, make_generator(config, handler))
    second_app = create_app(config, make_generator(config, handler))
    assert app is not second_app
    with TestClient(app, backend_options={'use_uvloop': True}):
        pass
    assert len([handler for handler in logging.getLogger('app').handlers
                if getattr(handler, '_vowframe_file_handler', False)]) == 1
    with caplog.at_level(logging.DEBUG):
        with TestClient(second_app, backend_options={'use_uvloop': True}) as client:
            project = create_project(client)
            batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
            wait_batch(client, batch['id'], status='needs_attention')
            task = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
            assert task['error_code'] == 'unknown_outcome'
            assert task['error_message'] == '模型服务连接中断，结果无法确认，可能已经扣费。重新生成可能产生额外费用'
    log_path = config.data_dir / 'logs' / 'vowframe.log'
    content = log_path.read_text(encoding='utf-8')
    assert 'ReadError' in content and 'ReadError' in caplog.text
    assert TEST_KEY not in content and TEST_KEY not in caplog.text
    managed = [handler for handler in logging.getLogger('app').handlers if getattr(handler, '_vowframe_file_handler', False)]
    assert len(managed) == 1
    assert managed[0].baseFilename == str(log_path)
    assert TEST_KEY not in caplog.text
    assert all(TEST_KEY not in repr(record.__dict__) for record in caplog.records)


def test_openai_data_uri_response(config, images):
    payload = {'data': [{'data_uri': 'data:image/png;base64,' + base64.b64encode(PNG).decode()}]}
    assert make_generator(config, lambda request: httpx.Response(200, json=payload)).generate_image(images, PROMPT, 'gpt-image-2', '3:4') == PNG


@pytest.mark.parametrize('failure', ['connect', 'timeout', 'read', 'write', 'protocol', 401, 403, 402, 429, 400, 422, 500, 502, 503, 504, 'empty'])
def test_download_failure_never_resubmits_paid_generation(config, images, failure):
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(200, json={'data': [{'url': 'https://images.example/result.png'}]})
        exceptions = {'connect': httpx.ConnectError, 'timeout': httpx.ReadTimeout, 'read': httpx.ReadError,
                      'write': httpx.WriteError, 'protocol': httpx.RemoteProtocolError}
        if failure in exceptions:
            raise exceptions[failure]('download failed')
        if failure == 'empty':
            return httpx.Response(200, content=b'')
        return httpx.Response(failure, text='download failed')

    with pytest.raises(GeneratorError) as raised:
        make_generator(config, handler).generate_image(images, PROMPT, 'gpt-image-2', '3:4')
    assert raised.value.kind == OutcomeKind.UNKNOWN_OUTCOME
    assert calls == ['POST', 'GET']


def test_missing_local_file_defaults_mock(monkeypatch):
    monkeypatch.delenv('VOWFRAME_GENERATOR')
    config = load_config()
    assert config.generator == 'mock'
    assert isinstance(create_app(config).state.generator, MockGenerator)


def test_local_config_and_environment_override(tmp_path, monkeypatch):
    path = tmp_path / 'custom-config.json'
    path.write_text(json.dumps({'generator': '88api', 'api_key': TEST_KEY, 'base_url': 'https://provider.example',
                               'model': 'gemini-nano-banana-2.1', 'timeout': 123, 'aspect_ratio': '3:4', 'resolution': '4K'}))
    monkeypatch.setenv('VOWFRAME_LOCAL_CONFIG', str(path))
    assert load_config().generator == 'mock'
    monkeypatch.delenv('VOWFRAME_GENERATOR')
    config = load_config()
    assert (config.generator, config.api_key, config.base_url, config.model, config.timeout, config.aspect_ratio, config.resolution) == (
        '88api', TEST_KEY, 'https://provider.example', 'gemini-nano-banana-2.1', 123, '3:4', '4K')
    assert isinstance(create_app(config).state.generator, Api88Generator)
    assert TEST_KEY not in repr(config)


@pytest.mark.parametrize('key', [None, '', '   '])
def test_missing_key_fails_at_startup(tmp_path, monkeypatch, key):
    path = tmp_path / 'config.local.json'
    data = {'generator': '88api'}
    if key is not None:
        data['api_key'] = key
    path.write_text(json.dumps(data))
    monkeypatch.setenv('VOWFRAME_LOCAL_CONFIG', str(path))
    monkeypatch.delenv('VOWFRAME_GENERATOR')
    with pytest.raises(ValueError, match='非空 api_key'):
        create_app()


def test_env_selecting_real_provider_requires_key(monkeypatch):
    monkeypatch.setenv('VOWFRAME_GENERATOR', '88api')
    with pytest.raises(ValueError, match='非空 api_key'):
        create_app()


@pytest.mark.parametrize('model', ['grok-imagine-image', 'midjourney-v8.2', 'doubao-seedream-5-0-pro', 'unknown'])
def test_unsupported_model_fails_at_startup(config, model):
    with pytest.raises(ValueError, match='暂不支持此模型'):
        create_app(replace(config, model=model))


@pytest.mark.parametrize('settings,message', [
    ({'timeout': 0}, 'timeout'), ({'timeout': float('inf')}, 'timeout'),
    ({'aspect_ratio': '99:1'}, 'aspect_ratio'), ({'resolution': '8K'}, 'resolution'),
    ({'base_url': 'not-a-url'}, 'base_url'),
    ({'base_url': 'https://user:password@example.com'}, 'base_url'),
])
def test_invalid_provider_settings_fail_before_requests(config, settings, message):
    with pytest.raises(ValueError, match=message):
        create_app(replace(config, **settings))


def test_invalid_local_json_does_not_echo_secret(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'config.local.json'
    path.write_text('{"api_key": "' + TEST_KEY + '"')
    monkeypatch.setenv('VOWFRAME_LOCAL_CONFIG', str(path))
    with pytest.raises(ValueError) as raised:
        create_app()
    assert TEST_KEY not in str(raised.value)
    assert TEST_KEY not in caplog.text


@pytest.mark.parametrize('role', ['bride', 'groom'])
def test_real_adapter_task_snapshot_health_and_logs_never_contain_key(config, role, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return image_response()

    app = create_app(config, make_generator(config, handler))
    with caplog.at_level(logging.DEBUG):
        with TestClient(app, backend_options={'use_uvloop': True}) as client:
            health = client.get('/api/health')
            assert health.json() == {
                'ok': True, 'generator': '88api', 'model': 'gpt-image-2',
                'face_swap': {'available': False, 'mode': None, 'reason': '未配置 FaceFusion 安装目录'},
            }
            assert TEST_KEY not in health.text
            project = create_project(client, role=role)
            batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
            wait_batch(client, batch['id'])
            with app.state.db.connect() as conn:
                row = conn.execute('SELECT * FROM generation_tasks WHERE batch_id=?', (batch['id'],)).fetchone()
                assert json.loads(row['model_config_snapshot']) == {
                    'generator': '88api', 'model': 'gpt-image-2', 'base_url': 'https://api.88api.ai',
                    'aspect_ratio': '3:4', 'resolution': '2K',
                }
                assert TEST_KEY not in repr(dict(row))
                expected = [project['person_a']['asset_id'], project['person_b']['asset_id']]
                assert json.loads(row['reference_image_ids']) == (expected if role == 'bride' else expected[::-1])
                file_names = [part[1] for part in multipart_parts(calls[0]) if part[1] is not None]
                ordered = json.loads(row['reference_image_ids'])
                assert [name.split('.')[0] for name in file_names] == ordered
            assert TEST_KEY not in client.get('/api/tasks').text
    assert len(calls) == 1
    assert TEST_KEY not in caplog.text
    assert all(TEST_KEY not in repr(record.__dict__) for record in caplog.records)


@pytest.mark.parametrize('failure,expected,calls_expected', [
    ('connect', 'completed', 2), ('read', 'interrupted', 1),
    (502, 'interrupted', 1), (504, 'interrupted', 1),
    (500, 'failed', 1), (503, 'failed', 1),
])
def test_adapter_outcomes_drive_existing_retry_policy(config, failure, expected, calls_expected):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            if isinstance(failure, int):
                return httpx.Response(failure, text='model service error')
            exception = httpx.ConnectError if failure == 'connect' else httpx.ReadTimeout
            raise exception('simulated network failure')
        return image_response()

    app = create_app(config, make_generator(config, handler))
    with TestClient(app, backend_options={'use_uvloop': True}) as client:
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='completed' if expected == 'completed' else 'needs_attention')
        task = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
        assert task['status'] == expected
        assert task['auto_retry_count'] == (1 if failure == 'connect' else 0)
        if expected != 'completed':
            assert task['error_code'] == ('unknown_outcome' if expected == 'interrupted' else 'rejected')
        if failure in (502, 504):
            assert task['error_message'] == '模型服务响应超时，结果无法确认，可能已经扣费。重新生成可能产生额外费用'
    assert len(calls) == calls_expected


def test_reference_photos_leave_without_metadata_and_upright(tmp_path):
    photo = tmp_path / 'phone.jpg'
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display
    exif[0x0110] = 'Phone Model X'
    exif.get_ifd(0x8825)[2] = (31.0, 14.0, 0.0)  # GPS latitude
    Image.new('RGB', (40, 30), 'white').save(photo, 'JPEG', exif=exif, xmp=b'<x:xmpmeta>secret</x:xmpmeta>')
    assert b'Phone Model X' in photo.read_bytes()

    name, content, mime = outgoing_image(photo)
    assert (name, mime) == ('phone.jpg', 'image/jpeg')
    assert b'Phone Model X' not in content and b'secret' not in content and b'Exif' not in content
    with Image.open(io.BytesIO(content)) as cleaned:
        assert cleaned.size == (30, 40)  # orientation applied to the pixels
        assert not cleaned.getexif()
