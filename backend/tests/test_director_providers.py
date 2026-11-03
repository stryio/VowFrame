"""The AI director has its own service choice and its own keys, separate from image generation."""


import httpx
import pytest

from app.config import Config
from app.director import Api88Director, DirectorFailed
from app.main import create_app
from fastapi.testclient import TestClient

IMAGE_88, IMAGE_PP = 'image-88-key', 'image-pp-key'
DIRECTOR_88, DIRECTOR_PP = 'director-88-key', 'director-pp-key'


def config_of(tmp_path, **keys):
    return Config(tmp_path / 'data', 1, '88api', 0, api_key=IMAGE_88, pptoken_api_key=IMAGE_PP, **keys)


def seen_request(director):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), key=request.headers['authorization'])
        return httpx.Response(200, json={'choices': [{'message': {'content': 'not json'}}]})
    director.transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError):
        director._call('hello')
    return seen


def test_each_service_gets_the_directors_own_key_and_address(tmp_path):
    config = config_of(tmp_path, director_api_key=DIRECTOR_88, director_pptoken_api_key=DIRECTOR_PP)
    assert seen_request(Api88Director(config, model='gpt-6.1-sol')) == {
        'url': 'https://api.88api.ai/v1/chat/completions', 'key': f'Bearer {DIRECTOR_88}'}
    assert seen_request(Api88Director(config, model='gpt-6.1-sol', provider='pptoken')) == {
        'url': 'https://api.pptoken.cc/v1/chat/completions', 'key': f'Bearer {DIRECTOR_PP}'}


@pytest.mark.parametrize('provider,name', [('88api', 'director_api_key'), ('pptoken', 'director_pptoken_api_key')])
def test_a_missing_director_key_is_an_error_and_never_borrows_the_image_key(tmp_path, provider, name):
    director = Api88Director(config_of(tmp_path), model='m', provider=provider)
    director.transport = httpx.MockTransport(lambda request: pytest.fail('nothing may be sent'))
    with pytest.raises(DirectorFailed, match=name):
        director._call('hello')


def client_of(config):
    return TestClient(create_app(config))


def test_settings_switch_the_director_without_touching_the_image_service(tmp_path):
    config = config_of(tmp_path, director_api_key=DIRECTOR_88, director_pptoken_api_key=DIRECTOR_PP, director_model='gpt-6.1-sol')
    with client_of(config) as client:
        before = client.get('/api/settings').json()
        assert (before['director_provider'], before['director_model']) == ('88api', 'gpt-6.1-sol')
        after = client.patch('/api/settings', json={'director_provider': 'pptoken'}).json()
        assert (after['director_provider'], after['director_model']) == ('pptoken', 'gpt-6.1-sol')
        assert after['image_provider'] == before['image_provider']
        director = client.app.state.director
        assert (director.provider, director.model) == ('pptoken', 'gpt-6.1-sol')
        # Each service remembers its own model.
        assert client.patch('/api/settings', json={'director_model': 'other'}).json()['director_model'] == 'other'
        back = client.patch('/api/settings', json={'director_provider': '88api'}).json()
        assert back['director_model'] == 'gpt-6.1-sol'
        assert client.patch('/api/settings', json={'director_provider': 'pptoken'}).json()['director_model'] == 'other'
    # The choice survives a restart.
    with client_of(config) as client:
        assert (client.app.state.director.provider, client.app.state.director.model) == ('pptoken', 'other')


def test_a_service_without_a_director_key_cannot_be_selected_even_if_its_image_key_exists(tmp_path):
    with client_of(config_of(tmp_path, director_api_key=DIRECTOR_88, director_model='gpt-6.1-sol')) as client:
        options = {item['id']: item for item in client.get('/api/settings').json()['director_providers']}
        assert options['88api']['available'] is True
        assert options['pptoken']['available'] is False and 'director_pptoken_api_key' in options['pptoken']['reason']
        refused = client.patch('/api/settings', json={'director_provider': 'pptoken'})
        assert refused.status_code == 409 and refused.json()['detail']['code'] == 'provider_unavailable'
        assert client.get('/api/settings').json()['image_providers'][1]['available'] is True
