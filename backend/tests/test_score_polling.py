"""Polling follows the actual background score lifecycle, independently of image generation."""

import sqlite3
import threading

import pytest

from app.db import Database, SCHEMA
from conftest import ScriptedGenerator, create_project, ensure_looks, preview_batch, wait_batch, wait_until
from test_face_swap import ScriptedFaceSwapper


class BlockingScorer(ScriptedFaceSwapper):
    def __init__(self, result=None, *, available=True):
        super().__init__()
        self.result = result
        self.available = available
        self.block = False
        self.started = threading.Event()
        self.release = threading.Event()

    @property
    def status(self):
        return {'available': self.available, 'mode': 'mock', 'reason': None}

    def score(self, items):
        if not self.block:
            return super().score(items)
        self.started.set()
        if not self.release.wait(10):
            raise RuntimeError('Test scorer was not released')
        if isinstance(self.result, Exception):
            raise self.result
        return [self.result for _ in items]


def settled_project(client, project_id):
    return wait_until(lambda: client.get(f'/api/projects/{project_id}').json(),
                      lambda detail: not detail['has_pending_face_scores'])


@pytest.mark.parametrize('result', [
    {'bride': 0.55, 'groom': 0.61},
    {'bride': None, 'groom': None},
    None,
    RuntimeError('Scoring failed'),
], ids=['scored', 'faces-not-found', 'no-result', 'failed'])
def test_project_keeps_polling_after_generation_until_scoring_finishes(app_factory, result):
    scorer = BlockingScorer(result)
    with app_factory(generator=ScriptedGenerator(), face_swapper=scorer) as (client, _app):
        project = create_project(client)
        other = create_project(client)
        ensure_looks(client, project['id'], ['city_story'])
        settled_project(client, project['id'])
        scorer.block = True
        try:
            batch = preview_batch(client, project['id'], style_ids=['city_story'])
            wait_batch(client, batch['id'])
            assert scorer.started.wait(2)
            detail = client.get(f"/api/projects/{project['id']}").json()
            assert detail['active_task_count'] == 0
            assert detail['previews'][0]['current_version']['face_scores'] is None
            assert detail['has_pending_face_scores'] is True
            assert client.get(f"/api/projects/{other['id']}").json()['has_pending_face_scores'] is False
        finally:
            scorer.release.set()
        detail = settled_project(client, project['id'])
        expected = None if isinstance(result, Exception) else result
        assert detail['previews'][0]['current_version']['face_scores'] == expected


@pytest.mark.parametrize('kind', ['makeup', 'look'])
def test_ready_candidates_keep_polling_until_their_score_is_written(app_factory, kind):
    scorer = BlockingScorer({'bride': 0.55, 'groom': 0.61})
    with app_factory(generator=ScriptedGenerator(), face_swapper=scorer) as (client, _app):
        project = create_project(client)
        if kind == 'look':
            ensure_looks(client, project['id'], ['city_story'])
            settled_project(client, project['id'])
        scorer.block = True
        payload = {'client_request_id': 'candidate-score', 'person': 'a', 'style_id': 'city_story'}
        if kind == 'makeup':
            payload['count'] = 1
            response = client.post(f"/api/projects/{project['id']}/makeups", json=payload)
            key = 'makeups'
        else:
            payload['items'] = [{'hairstyle_id': 'keep'}]
            response = client.post(f"/api/projects/{project['id']}/looks", json=payload)
            key = 'looks'
        try:
            assert response.status_code == 201, response.text
            candidate_id = response.json()[0]['id']
            assert scorer.started.wait(2)
            detail = client.get(f"/api/projects/{project['id']}").json()
            candidate = next(item for item in detail[key] if item['id'] == candidate_id)
            assert candidate['status'] == 'ready' and candidate['face_score'] is None
            assert detail['active_task_count'] == 0 and detail['has_pending_face_scores'] is True
        finally:
            scorer.release.set()
        detail = settled_project(client, project['id'])
        assert next(item for item in detail[key] if item['id'] == candidate_id)['face_score'] == 0.55


@pytest.mark.parametrize('available', [True, False])
def test_restart_rebuilds_pending_scores_and_clears_them_when_scoring_is_unavailable(app_factory, tmp_path, available):
    data_dir = tmp_path / 'restart'
    with app_factory(face_swapper=BlockingScorer(available=False), data_dir=data_dir) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        wait_batch(client, batch['id'])
        detail = client.get(f"/api/projects/{project['id']}").json()
        assert detail['has_pending_face_scores'] is False
        # Simulate scoring interrupted while its persisted pending marker was set.
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE photo_versions SET face_score_pending=1')
    scorer = BlockingScorer({'bride': 0.55, 'groom': 0.61}, available=available)
    scorer.block = True
    with app_factory(face_swapper=scorer, data_dir=data_dir) as (client, _app):
        try:
            if available:
                assert scorer.started.wait(2)
            detail = client.get(f"/api/projects/{project['id']}").json()
            assert detail['has_pending_face_scores'] is available
        finally:
            scorer.release.set()
        detail = settled_project(client, project['id'])
        expected = {'bride': 0.55, 'groom': 0.61} if available else None
        assert detail['previews'][0]['current_version']['face_scores'] == expected


def test_existing_database_gets_pending_score_columns_without_losing_projects(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('kept','Kept project','before','before')")
    db = Database(path)
    db.initialize()
    db.initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT name FROM projects WHERE id='kept'").fetchone()[0] == 'Kept project'
        for table in ('photo_versions', 'looks', 'makeups'):
            column = next(row for row in conn.execute(f'PRAGMA table_info({table})') if row['name'] == 'face_score_pending')
            assert column['notnull'] == 1 and column['dflt_value'] == '0'
