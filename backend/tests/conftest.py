"""Isolated local apps and deterministic provider controls."""

import base64
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

# Set before importing app.main: its module-level app must never select a paid
# provider from a developer's local configuration during test collection.
os.environ['VOWFRAME_GENERATOR'] = 'mock'
os.environ['VOWFRAME_LOCAL_CONFIG'] = str(Path(__file__).resolve().parent / 'config.local.test-missing.json')

from app.config import Config
from app.main import create_app

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/EcAAAAASUVORK5CYII=')


@pytest.fixture(autouse=True)
def force_mock_generator(monkeypatch, tmp_path):
    monkeypatch.setenv('VOWFRAME_GENERATOR', 'mock')
    monkeypatch.setenv('VOWFRAME_LOCAL_CONFIG', str(tmp_path / 'config.local.json'))
    monkeypatch.delenv('VOWFRAME_FACE_SWAPPER', raising=False)

    def forbid_network(*args, **kwargs):
        raise AssertionError('Tests must use an injected transport; real network calls are forbidden')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbid_network)


class ScriptedGenerator:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []
        self.models = []
        self.aspect_ratios = []
        self.lock = threading.Lock()

    def generate_image(self, images: list[Path], prompt: str, model: str, aspect_ratio: str) -> bytes:
        with self.lock:
            self.calls.append((list(images), prompt, time.monotonic()))
            self.models.append(model)
            self.aspect_ratios.append(aspect_ratio)
            outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return images[0].read_bytes()


class BlockingGenerator(ScriptedGenerator):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.models = []

    def generate_image(self, images: list[Path], prompt: str, model: str, aspect_ratio: str) -> bytes:
        with self.lock:
            self.calls.append((list(images), prompt, time.monotonic()))
            self.models.append(model)
            self.aspect_ratios.append(aspect_ratio)
        self.started.set()
        if not self.release.wait(10):
            raise RuntimeError('Test generator was not released')
        return images[0].read_bytes()


@pytest.fixture
def app_factory(tmp_path):
    @contextmanager
    def factory(*, generator=None, face_swapper=None, concurrency=2, data_dir=None, auto_face_swap=False):
        config = Config(data_dir or tmp_path / 'data', concurrency, 'mock', 0)
        app = create_app(config, generator, face_swapper)
        # uvicorn[standard] uses uvloop on this Linux/Python 3.14 environment.
        # Match that loop in TestClient to avoid the stdlib portal wake-up stall.
        with TestClient(app, backend_options={'use_uvloop': True}) as client:
            # Most tests count tasks of one kind; automatic face swap is opted into where it is the subject.
            app.state.db.set_auto_face_swap(auto_face_swap)
            try:
                yield client, app
            finally:
                if isinstance(generator, BlockingGenerator):
                    generator.release.set()
    return factory


@pytest.fixture
def client_app(app_factory):
    with app_factory() as pair:
        yield pair


def create_project(client, *, upload=True, role='bride'):
    response = client.post('/api/projects', json={})
    assert response.status_code == 201, response.text
    project = response.json()
    if role != 'bride':
        project = client.patch(f"/api/projects/{project['id']}", json={'person_a_role': role}).json()
    if upload:
        for person in ('a', 'b'):
            response = client.post(f"/api/projects/{project['id']}/images", data={'person': person}, files={'file': (person + '.png', PNG, 'image/png')})
            assert response.status_code == 200, response.text
            project = response.json()
    return project


# Batch-mechanics tests count tasks; pin them to six styles so adding styles does not change their numbers.
DEFAULT_TEST_STYLE_IDS = ['korean_minimal', 'french_vintage', 'forest_natural', 'seaside_sunset', 'chinese_traditional', 'cinematic_light']


def preview_batch(client, project_id, *, request_id='request-1', style_ids=None, model=None):
    body = {'client_request_id': request_id, 'style_ids': style_ids or DEFAULT_TEST_STYLE_IDS}
    if model is not None:
        body['model'] = model
    response = client.post(f'/api/projects/{project_id}/previews', json=body)
    assert response.status_code == 201, response.text
    return response.json()


def create_director_photo_batch(client, project_id, preview_slot_id, *, request_id, count=4, model=None):
    draft_response = client.post(f'/api/projects/{project_id}/moment-drafts', json={
        'preview_slot_id': preview_slot_id, 'count': count,
    })
    assert draft_response.status_code == 201, draft_response.text
    body = {'client_request_id': request_id, 'draft_id': draft_response.json()['id']}
    if model is not None:
        body['model'] = model
    return client.post(f'/api/projects/{project_id}/photo-batches', json=body)


def wait_until(callback, predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = callback()
        if predicate(value):
            return value
        time.sleep(.01)
    raise AssertionError(f'Timed out waiting for expected state; last value: {value}')


def wait_batch(client, batch_id, *, status='completed'):
    return wait_until(lambda: client.get(f'/api/batches/{batch_id}').json(), lambda batch: batch['status'] == status)
