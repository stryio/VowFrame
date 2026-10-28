"""Optional frontend hosting must not intercept backend endpoints."""

from unittest.mock import patch


def test_spa_fallback_and_static_files_preserve_api(app_factory, tmp_path):
    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'assets').mkdir()
    (dist / 'index.html').write_text('<html>VowFrame</html>')
    (dist / 'assets' / 'app.js').write_text('export const ready = true;')
    with patch('app.main.FRONTEND_DIST', dist):
        with app_factory() as (client, app):
            assert client.get('/').text == '<html>VowFrame</html>'
            assert client.get('/projects/abc').text == '<html>VowFrame</html>'
            assert client.get('/assets/app.js').text == 'export const ready = true;'
            assert client.get('/assets/missing.js').status_code == 404
            assert client.get('/api/health').json() == {
                'ok': True, 'generator': 'mock', 'model': 'gpt-image-2',
                'face_swap': {'available': False, 'mode': None, 'reason': '未配置 FaceFusion 安装目录'},
            }
            for path in ('/api', '/api/missing', '/api/projects/missing', '/%2e%2e/private'):
                response = client.get(path)
                assert response.status_code == 404
                assert response.json()['detail']['message']


def test_missing_frontend_returns_json_404(app_factory, tmp_path):
    with patch('app.main.FRONTEND_DIST', tmp_path / 'missing'):
        with app_factory() as (client, app):
            assert client.get('/').status_code == 404
            assert client.get('/api/health').status_code == 200
