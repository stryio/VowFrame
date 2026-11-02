"""The AI face fix (V7.1; the whole picture since V7.2): one edit of a finished picture with the bride's made-up photo
and the groom's look (V7.9), kept as the slot's new version; it runs after every generated picture with faces unless the setting says otherwise."""

import json
import re
import threading
from pathlib import Path

import pytest

from app.face_swap import FaceSwapError
from app.generator import GeneratorError
from app.prompts import FACE_FIX_BRIDE_MAKEUP_PROMPT, FACE_FIX_BRIDE_PROMPT, FACE_FIX_MAKEUP_PROMPT, FACE_FIX_PROMPT
from conftest import BlockingGenerator, ScriptedGenerator, create_project, preview_batch, wait_batch, wait_until
from test_face_swap import ScriptedFaceSwapper


def fix_task(client, batch_id):
    return next(task for task in client.get('/api/tasks', params={'batch_id': batch_id}).json() if task['regenerate_mode'] == 'face_fix')


def test_the_prompt_assigns_each_attribute_to_its_image_and_matches_the_docs():
    assert FACE_FIX_PROMPT.startswith('第一张图是需要修正的婚纱照。第二张图是新娘的造型照，第三张图是新郎的造型照')
    # V7.4: hair, makeup and skin tone follow the looks, so the face matches the body.
    assert '长相依据造型照' in FACE_FIX_PROMPT and '第一张图中已经符合造型照的妆容、发型和肤色保持不动' in FACE_FIX_PROMPT
    assert '不替换整颗头' in FACE_FIX_PROMPT and '保留本人固有的骨相与脸部比例' in FACE_FIX_PROMPT
    assert '是同一种肤色' in FACE_FIX_PROMPT and '原始照片' not in FACE_FIX_PROMPT and '换脸' not in FACE_FIX_PROMPT
    # V7.9: the bride's made-up photo gives her face and makeup, never her hair.
    assert FACE_FIX_MAKEUP_PROMPT.startswith('第一张图是需要修正的婚纱照。第二张图是新娘婚礼当天化好妆、还没有做发型时的照片')
    assert '新娘的头发不参考第二张图' in FACE_FIX_MAKEUP_PROMPT and '两人的发型' not in FACE_FIX_MAKEUP_PROMPT
    assert '新郎的发型、发色和皮肤的白皙程度与第三张图相同' in FACE_FIX_MAKEUP_PROMPT and '不替换整颗头' in FACE_FIX_MAKEUP_PROMPT
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
    section = re.search(r'### 9\.2 编辑提示词(.*?)### 9\.3', doc, re.S).group(1)
    assert re.findall(r'```text\n(.*?)\n```', section, re.S) == [FACE_FIX_MAKEUP_PROMPT, FACE_FIX_PROMPT]


def test_every_picture_gets_one_ai_face_fix_of_the_whole_picture_with_its_looks(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, auto_face_fix='ai', concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 2
        _shot, (images, prompt, _at) = generator.calls
        assert prompt == FACE_FIX_MAKEUP_PROMPT and generator.aspect_ratios == ['3:4', '3:4']
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        with app.state.db.connect() as conn:
            bride = conn.execute('SELECT makeup_asset_id FROM looks WHERE id=?', (slot['look_ids']['bride'],)).fetchone()[0]
            groom = conn.execute('SELECT image_asset_id FROM looks WHERE id=?', (slot['look_ids']['groom'],)).fetchone()[0]
        # The picture, the bride's made-up photo (V7.9) and the groom's look (V7.4), never the bare uploaded photos.
        looks = [bride, groom]
        assert bride and [path.stem for path in images[1:]] == looks
        task = fix_task(client, batch['id'])
        with app.state.db.connect() as conn:
            row = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
            source = conn.execute('SELECT asset.file_path FROM photo_versions version JOIN image_assets asset '
                                  'ON asset.id=version.image_asset_id WHERE version.id=?', (row['source_version_id'],)).fetchone()
        assert images[0].as_posix().endswith(source['file_path'])
        assert json.loads(row['reference_image_ids']) == looks
        assert json.loads(row['model_config_snapshot']) == {'generator': 'face_fix', 'model': 'gpt-image-2', 'aspect_ratio': '3:4',
                                                            'bride_reference': 'makeup'}
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert slot['current_version']['version_number'] == 2 and slot['current_version']['source_version_id']
        assert slot['look_ids'] is not None                     # the fixed picture keeps its sample's looks


def test_a_bride_look_without_a_made_up_photo_is_the_reference_with_the_v74_prompt(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        # A look from before V7.5 had no made-up step.
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE looks SET makeup_asset_id=NULL WHERE id=?', (slot['look_ids']['bride'],))
            look = conn.execute('SELECT image_asset_id FROM looks WHERE id=?', (slot['look_ids']['bride'],)).fetchone()[0]
        response = client.post(f"/api/slots/{slot['id']}/face-fix", json={'client_request_id': 'old-look'})
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        images, prompt, _at = generator.calls[-1]
        assert prompt == FACE_FIX_PROMPT and images[1].stem == look


@pytest.mark.parametrize('mode', ['ai', 'split'])
def test_a_failed_fix_keeps_the_original(app_factory, mode):
    generator = ScriptedGenerator([None, GeneratorError('rejected')])
    with app_factory(generator=generator, face_swapper=ScriptedFaceSwapper(), auto_face_fix=mode, concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        assert fix_task(client, batch['id'])['error_code'] == 'rejected'
        assert not any(task['regenerate_mode'] == 'face_swap' for task in client.get('/api/tasks', params={'batch_id': batch['id']}).json())
        assert client.get(f"/api/projects/{project['id']}").json()['previews'][0]['current_version']['version_number'] == 1


def test_the_setting_picks_the_ai_fix_the_swap_or_nothing(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_fix='off', concurrency=1) as (client, _app):
        assert client.get('/api/settings').json()['auto_face_fix'] == 'off'
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 1
        assert client.patch('/api/settings', json={'auto_face_fix': 'facefusion'}).json()['auto_face_fix'] == 'facefusion'
        batch = preview_batch(client, project['id'], request_id='swap', style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        modes = {task['regenerate_mode'] for task in client.get('/api/tasks', params={'batch_id': batch['id']}).json()}
        assert modes == {None, 'face_swap'} and swapper.calls
        assert client.patch('/api/settings', json={'auto_face_fix': 'sometimes'}).status_code == 422


def test_a_picture_can_be_fixed_by_hand_and_a_fix_starts_from_the_original(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        original = slot['current_version']['id']
        for request_id in ('first', 'again'):
            response = client.post(f"/api/slots/{slot['id']}/face-fix", json={'client_request_id': request_id})
            assert response.status_code == 201, response.text
            wait_batch(client, response.json()['id'])
            # Fixing a fix fixes the original picture again.
            with app.state.db.connect() as conn:
                row = conn.execute('SELECT source_version_id FROM generation_tasks WHERE batch_id=?', (response.json()['id'],)).fetchone()
            assert row['source_version_id'] == original
        bad = client.post(f"/api/slots/{slot['id']}/face-fix", json={'client_request_id': 'bad', 'model': 'nope'})
        assert bad.status_code == 422 and bad.json()['detail']['code'] == 'invalid_model'
        batch = client.post('/api/slots/batch-action', json={'client_request_id': 'many', 'slot_ids': [slot['id']], 'action': 'face_fix'})
        assert batch.status_code == 201, batch.text


class FallingScores(ScriptedFaceSwapper):
    """Every picture scored after another one has a less alike groom: the AI fix (scored after the picture it fixed)
    makes him less alike."""

    def score(self, items):
        result = []
        for item in items:
            self.scored.append(item)
            result.append({'bride': 0.6, 'groom': round(0.9 - 0.05 * len(self.scored), 3)})
        return result


def test_a_fix_that_makes_someone_less_alike_keeps_the_original(app_factory):
    with app_factory(generator=ScriptedGenerator(), face_swapper=FallingScores(), auto_face_fix='ai', concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        slot = wait_until(lambda: client.get(f"/api/projects/{project['id']}").json()['previews'][0],
                          lambda slot: slot['current_version']['version_number'] == 1)
        assert slot['version_count'] == 2                        # the fix stays in the history
        with app.state.db.connect() as conn:
            row = conn.execute("SELECT model_config_snapshot FROM generation_tasks WHERE regenerate_mode='face_fix'").fetchone()
        assert json.loads(row[0])['kept_original'] == ['groom']


def test_a_fix_that_keeps_the_likeness_stays(app_factory):
    with app_factory(generator=ScriptedGenerator(), face_swapper=ScriptedFaceSwapper(), auto_face_fix='ai', concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        wait_until(lambda: client.get(f"/api/projects/{project['id']}").json()['previews'][0],
                   lambda slot: slot['current_version']['face_scores'] is not None)
        assert client.get(f"/api/projects/{project['id']}").json()['previews'][0]['current_version']['version_number'] == 2


class ScoredSwapper(ScriptedFaceSwapper):
    """FaceFusion for the scores and the groom swap; a picture's bride score comes from a script by version order."""

    def __init__(self, bride_scores):
        super().__init__()
        self.bride_scores = list(bride_scores)

    def score(self, items):
        self.scored.extend(items)
        # Pictures are compared with both uploads; looks and makeups with one photo (bride == groom).
        return [{'bride': self.bride_scores.pop(0) if self.bride_scores and item.bride != item.groom else 0.6, 'groom': 0.6}
                for item in items]


def test_the_bride_only_prompts_leave_the_groom_and_match_the_docs():
    for prompt in (FACE_FIX_BRIDE_MAKEUP_PROMPT, FACE_FIX_BRIDE_PROMPT):
        assert '请只校正照片里新娘的长相' in prompt and '新郎不在这次修正范围内' in prompt and '第三张图' not in prompt
        assert '新郎的脸、发型、肤色、表情和全身' in prompt and '不替换整颗头' in prompt
    assert '新娘的头发不参考第二张图' in FACE_FIX_BRIDE_MAKEUP_PROMPT and '新娘的妆色、妆面、提白程度、发型和发色和造型照相同' in FACE_FIX_BRIDE_PROMPT
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
    section = re.search(r'### 9\.4 只修新娘(.*)', doc, re.S).group(1)
    assert re.findall(r'```text\n(.*?)\n```', section, re.S) == [FACE_FIX_BRIDE_MAKEUP_PROMPT, FACE_FIX_BRIDE_PROMPT]


def test_split_fixes_the_bride_by_ai_then_swaps_only_the_groom_with_facefusion(app_factory):
    generator, swapper = ScriptedGenerator(), ScoredSwapper([])
    with app_factory(generator=generator, face_swapper=swapper, auto_face_fix='split', concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_until(lambda: client.get(f"/api/batches/{batch['id']}").json(),
                   lambda batch: batch['total'] == 3 and batch['status'] == 'completed')
        modes = [task['regenerate_mode'] for task in sorted(client.get('/api/tasks', params={'batch_id': batch['id']}).json(),
                                                            key=lambda task: task['created_at'])]
        assert modes == [None, 'face_fix', 'face_swap']
        # The AI edit gets the picture and the bride's reference only, with the bride-only prompt.
        images, prompt, _at = generator.calls[1]
        assert prompt == FACE_FIX_BRIDE_MAKEUP_PROMPT and len(images) == 2
        # FaceFusion swaps only the groom, on the fixed picture, which becomes the slot's picture.
        assert swapper.roles == [('groom',)]
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert slot['current_version']['version_number'] == 3
        with app.state.db.connect() as conn:
            swap = conn.execute("SELECT * FROM generation_tasks WHERE regenerate_mode='face_swap'").fetchone()
            fix_version = conn.execute("SELECT version.id FROM photo_versions version JOIN generation_tasks task ON "
                                       "task.id=version.generation_task_id WHERE task.regenerate_mode='face_fix'").fetchone()[0]
        assert swap['source_version_id'] == fix_version and json.loads(swap['model_config_snapshot'])['roles'] == ['groom']
        assert set(json.loads(swap['model_config_snapshot'])['faces']) == {'groom'}
        assert slot['look_ids'] is not None and not slot['is_stale']
        assert swapper.calls[0][1].front.stem == slot['styled_asset_ids']['groom']


def test_split_swaps_the_groom_on_the_original_when_the_fix_made_the_bride_less_alike(app_factory):
    # Scored in order: the sample (0.6), then the fix (0.5, worse), then the swapped picture.
    generator, swapper = ScriptedGenerator(), ScoredSwapper([0.6, 0.5])
    with app_factory(generator=generator, face_swapper=swapper, auto_face_fix='split', concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_until(lambda: client.get(f"/api/batches/{batch['id']}").json(),
                   lambda batch: batch['total'] == 3 and batch['status'] == 'completed')
        with app.state.db.connect() as conn:
            swap = conn.execute("SELECT source_version_id FROM generation_tasks WHERE regenerate_mode='face_swap'").fetchone()
            original = conn.execute("SELECT version.id FROM photo_versions version JOIN generation_tasks task ON "
                                    "task.id=version.generation_task_id WHERE task.regenerate_mode IS NULL").fetchone()[0]
            fix = conn.execute("SELECT model_config_snapshot FROM generation_tasks WHERE regenerate_mode='face_fix'").fetchone()[0]
        assert swap['source_version_id'] == original and json.loads(fix)['kept_original'] == ['bride']


class BlockingFixGenerator(BlockingGenerator):
    """Let the shot finish, then wait while the bride's AI edit is running."""

    def generate_image(self, images, prompt, model, aspect_ratio):
        content = ScriptedGenerator.generate_image(self, images, prompt, model, aspect_ratio)
        if len(self.calls) == 2:
            self.started.set()
            if not self.release.wait(10):
                raise RuntimeError('Test bride fix was not released')
        return content


class BlockingFixScorer(ScoredSwapper):
    def __init__(self):
        super().__init__([])
        self.started = threading.Event()
        self.release = threading.Event()
        self.pictures = 0

    def score(self, items):
        self.pictures += sum(item.bride != item.groom for item in items)
        if self.pictures >= 2 and not self.started.is_set():
            self.started.set()
            if not self.release.wait(10):
                raise RuntimeError('Test fix scoring was not released')
        return super().score(items)


def test_split_stays_active_during_scoring_and_stopping_it_cannot_recreate_the_groom_task(app_factory):
    swapper = BlockingFixScorer()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_fix='split', concurrency=1) as (client, _app):
        try:
            project = create_project(client)
            batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
            assert swapper.started.wait(5)
            state = client.get(f"/api/batches/{batch['id']}").json()
            assert state['total'] == 3 and state['status'] == 'running'
            slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
            busy = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'during-scoring', 'mode': 'same'})
            assert busy.status_code == 409 and busy.json()['detail']['code'] == 'slot_busy'
            stopped = client.post(f"/api/batches/{batch['id']}/stop").json()
            assert stopped['counts']['cancelled'] == 1
        finally:
            swapper.release.set()
        wait_until(lambda: client.get(f"/api/slots/{slot['id']}/versions").json(),
                   lambda versions: all(version['face_scores'] is not None for version in versions))
        assert swapper.calls == []
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 3


def test_split_pauses_the_groom_follow_up_and_continues_it_after_a_restart(app_factory, tmp_path):
    generator, swapper = BlockingFixGenerator(), ScoredSwapper([])
    data_dir = tmp_path / 'split-restart'
    with app_factory(generator=generator, face_swapper=swapper, auto_face_fix='split', concurrency=1,
                     data_dir=data_dir) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        assert generator.started.wait(5)
        client.post(f"/api/batches/{batch['id']}/pause").raise_for_status()
        generator.release.set()
        paused = wait_until(lambda: client.get(f"/api/batches/{batch['id']}").json(),
                            lambda value: value['total'] == 3 and value['status'] == 'held')
        assert paused['paused'] and paused['counts']['held'] == 1 and not swapper.calls
        groom_task = next(task for task in client.get('/api/tasks', params={'batch_id': batch['id']}).json()
                          if task['regenerate_mode'] == 'face_swap')
        single = client.post(f"/api/tasks/{groom_task['id']}/continue")
        assert single.status_code == 409 and single.json()['detail']['code'] == 'batch_paused'

    resumed_generator, resumed_swapper = ScriptedGenerator(), ScoredSwapper([])
    with app_factory(generator=resumed_generator, face_swapper=resumed_swapper, concurrency=1,
                     data_dir=data_dir) as (client, _app):
        assert client.get(f"/api/batches/{batch['id']}").json()['status'] == 'held'
        assert not resumed_swapper.calls
        client.post(f"/api/batches/{batch['id']}/continue").raise_for_status()
        assert wait_batch(client, batch['id'])['total'] == 3
        assert resumed_swapper.roles == [('groom',)] and not resumed_generator.calls


@pytest.mark.parametrize('role', ['bride', 'groom'])
def test_split_without_facefusion_uses_only_the_brides_reference_even_when_people_are_swapped(app_factory, role):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, auto_face_fix='split', concurrency=1) as (client, app):
        project = create_project(client, role=role)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        assert wait_batch(client, batch['id'])['total'] == 2
        images, prompt, _at = generator.calls[-1]
        task = fix_task(client, batch['id'])
        with app.state.db.connect() as conn:
            references = json.loads(conn.execute('SELECT reference_image_ids FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()[0])
        assert len(images) == 2 and images[1].stem == references[0] and prompt == FACE_FIX_BRIDE_MAKEUP_PROMPT
        assert client.get(f"/api/projects/{project['id']}").json()['previews'][0]['current_version']['version_number'] == 2


def test_split_keeps_the_chain_when_similarity_scoring_fails(app_factory):
    class FailingScorer(ScriptedFaceSwapper):
        def score(self, items):
            if any(item.bride != item.groom for item in items):
                raise RuntimeError('Scoring is unavailable')
            return super().score(items)

    generator, swapper = ScriptedGenerator(), FailingScorer()
    with app_factory(generator=generator, face_swapper=swapper, auto_face_fix='split', concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        assert wait_batch(client, batch['id'])['total'] == 3
        assert len(generator.calls) == 2 and swapper.roles == [('groom',)]


def test_retrying_a_failed_groom_swap_does_not_repeat_the_paid_bride_fix(app_factory):
    generator, swapper = ScriptedGenerator(), ScriptedFaceSwapper([FaceSwapError('face_not_found')])
    with app_factory(generator=generator, face_swapper=swapper, auto_face_fix='split', concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        swap = next(task for task in client.get('/api/tasks', params={'batch_id': batch['id']}).json()
                    if task['regenerate_mode'] == 'face_swap')
        assert swap['error_code'] == 'face_not_found'
        client.post(f"/api/batches/{batch['id']}/pause").raise_for_status()
        retry = client.post(f"/api/tasks/{swap['id']}/retry")
        retry.raise_for_status()
        assert retry.json()['status'] == 'held'
        client.post(f"/api/batches/{batch['id']}/continue").raise_for_status()
        wait_batch(client, batch['id'])
        assert len(generator.calls) == 2 and swapper.roles == [('groom',), ('groom',)]


def test_manual_corrections_after_split_start_from_the_generated_picture_and_its_looks(app_factory):
    generator, swapper = ScriptedGenerator(), ScoredSwapper([])
    with app_factory(generator=generator, face_swapper=swapper, auto_face_fix='split', concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        original = next(version for version in client.get(f"/api/slots/{slot['id']}/versions").json() if version['version_number'] == 1)
        for action in ('face-fix', 'face-swap'):
            response = client.post(f"/api/slots/{slot['id']}/{action}", json={'client_request_id': action})
            assert response.status_code == 201, response.text
            wait_batch(client, response.json()['id'])
            task = client.get('/api/tasks', params={'batch_id': response.json()['id']}).json()[0]
            assert task['source_version_id'] == original['id']
        assert generator.calls[-1][1] == FACE_FIX_MAKEUP_PROMPT
        assert swapper.roles[-1] == ('bride', 'groom')
        assert swapper.calls[-1][0].front.stem == slot['styled_asset_ids']['bride']
