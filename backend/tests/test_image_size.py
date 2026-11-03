"""A service can return another size than requested: the picture is kept, and the user is told."""

import io

from fastapi.testclient import TestClient
from PIL import Image

from app.api88 import size_problem
from app.config import Config
from app.main import create_app
from conftest import PassthroughPhotoPreparer, ScriptedGenerator, create_project, preview_batch, wait_batch
from test_face_swap import ScriptedFaceSwapper
from test_styling import detail, make_looks


def png(width, height):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), 'white').save(buffer, 'PNG')
    return buffer.getvalue()


class SizedGenerator(ScriptedGenerator):
    def __init__(self, size):
        super().__init__()
        self.size = size

    def generate_image(self, images, prompt, model, aspect_ratio):
        super().generate_image(images, prompt, model, aspect_ratio)
        return png(*self.size)


def test_size_problem_compares_exact_pixels_for_openai_models_only():
    assert size_problem(png(1536, 2048), 'gpt-image-2.5', '3:4', '2K') is None
    assert size_problem(png(1086, 1448), 'gpt-image-2.5', '3:4', '2K') == '要求 1536×2048，实际 1086×1448'
    assert size_problem(png(1086, 1448), 'gpt-image-2.5', '3:4', '4K') == '要求 2448×3264，实际 1086×1448'
    assert size_problem(png(1086, 1448), 'gemini-nano-banana-2.1', '3:4', '2K') is None
    assert '无法读取' in size_problem(b'not a picture', 'gpt-image-2.5', '3:4', '2K')


def sample_version(tmp_path, size):
    config = Config(tmp_path / 'data', 1, '88api', 0, api_key='k', director_api_key='d')
    generator = SizedGenerator(size)
    app = create_app(config, generator, photo_preparer=PassthroughPhotoPreparer(), stylist=ScriptedGenerator(),
                     face_swapper=ScriptedFaceSwapper())
    return app


def test_a_wrong_size_is_kept_shown_and_passed_on_to_a_face_swap(tmp_path):
    with TestClient(sample_version(tmp_path, (1086, 1448))) as client:
        client.patch('/api/settings', json={'auto_face_fix': 'off'})
        project = create_project(client)
        make_looks(client, project['id'], 'a', [{'hairstyle_id': 'low_bun'}], style_id='korean_minimal')
        make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}])
        batch = preview_batch(client, project['id'], request_id='s', style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        slot = detail(client, project['id'])['previews'][0]
        assert slot['current_version']['size_warning'] == '要求 1536×2048，实际 1086×1448'
        versions = client.get(f"/api/slots/{slot['id']}/versions").json()
        assert versions[0]['size_warning'] == '要求 1536×2048，实际 1086×1448'
        swap = client.post(f"/api/slots/{slot['id']}/face-swap", json={'client_request_id': 'swap'})
        assert swap.status_code == 201, swap.text
        wait_batch(client, swap.json()['id'])
        assert detail(client, project['id'])['previews'][0]['current_version']['version_number'] == 2
        assert detail(client, project['id'])['previews'][0]['current_version']['size_warning'] == '要求 1536×2048，实际 1086×1448'


def test_the_right_size_has_no_warning(tmp_path):
    with TestClient(sample_version(tmp_path, (1536, 2048))) as client:
        client.patch('/api/settings', json={'auto_face_fix': 'off'})
        project = create_project(client)
        make_looks(client, project['id'], 'a', [{'hairstyle_id': 'low_bun'}], style_id='korean_minimal')
        make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}])
        batch = preview_batch(client, project['id'], request_id='s', style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        assert detail(client, project['id'])['previews'][0]['current_version']['size_warning'] is None
