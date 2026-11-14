"""The AI face fix (V7.1; the whole picture since V7.2): one edit of a finished picture with both looks
(V7.14), kept as the slot's new version; it runs after every generated picture with faces unless the setting says otherwise."""

import json
import re
import threading
from pathlib import Path

import pytest

from app.face_fix import face_fix_prompt, face_fix_snapshot
from app.face_swap import FaceSwapError
from app.generator import GeneratorError
from app.prompts import (ATMOSPHERE_QUALITY_PARAGRAPH, FACE_FIX_BRIDE_MAKEUP_PROMPT, FACE_FIX_BRIDE_PROMPT, FACE_FIX_GROOM_PROMPT, FACE_FIX_MAKEUP_PROMPT,
                         FACE_FIX_PROMPT, HAND_FIX_PROMPT, QUALITY_PARAGRAPH, WASH_PROMPT, WASH_REPAIR_PROMPT)
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
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        wait_batch(client, batch['id'])
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 2
        _shot, (images, prompt, _at) = generator.calls
        assert prompt == (FACE_FIX_PROMPT) and generator.aspect_ratios == ['3:4', '3:4']
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        with app.state.db.connect() as conn:
            bride, made_up = conn.execute('SELECT image_asset_id,makeup_asset_id FROM looks WHERE id=?', (slot['look_ids']['bride'],)).fetchone()
            groom = conn.execute('SELECT image_asset_id FROM looks WHERE id=?', (slot['look_ids']['groom'],)).fetchone()[0]
        # The picture and both looks, never the uploaded photos or (V7.14) the bride's made-up photo with its old hair.
        looks = [bride, groom]
        assert made_up and made_up not in looks and [path.stem for path in images[1:]] == looks
        task = fix_task(client, batch['id'])
        with app.state.db.connect() as conn:
            row = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
            source = conn.execute('SELECT asset.file_path FROM photo_versions version JOIN image_assets asset '
                                  'ON asset.id=version.image_asset_id WHERE version.id=?', (row['source_version_id'],)).fetchone()
        assert images[0].as_posix().endswith(source['file_path'])
        assert json.loads(row['reference_image_ids']) == looks
        # V7.27: the fix records the service it goes to.
        assert json.loads(row['model_config_snapshot']) == {'generator': 'face_fix', 'model': 'gpt-image-2', 'aspect_ratio': '3:4',
                                                            'bride_reference': 'look', 'provider': '88api'}
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert slot['current_version']['version_number'] == 2 and slot['current_version']['source_version_id']
        assert slot['look_ids'] is not None                     # the fixed picture keeps its sample's looks


def test_tasks_recorded_with_the_made_up_photo_keep_their_prompts():
    # Their reference_image_ids hold the made-up photo, so a retry must not tell the model it is the look.
    assert face_fix_prompt({'bride_reference': 'makeup'}) == FACE_FIX_MAKEUP_PROMPT
    assert face_fix_prompt({'bride_reference': 'makeup', 'roles': ['bride']}) == FACE_FIX_BRIDE_MAKEUP_PROMPT
    assert face_fix_prompt({}) == FACE_FIX_PROMPT
    # V7.32–V7.77 tasks recording the retouch also fix the hands and wash, before the closing paragraph.
    combined = face_fix_prompt({'retouch': ['hands', 'wash'], 'roles': ['groom']})
    assert combined.startswith(FACE_FIX_GROOM_PROMPT.rsplit('\n\n', 1)[0]) and combined.endswith(FACE_FIX_GROOM_PROMPT.rsplit('\n\n', 1)[1])
    assert '五根手指' in combined and '重绘并清晰化' in combined
    # V7.78: new face fixes edit only the faces, at the configured size.
    snapshot = json.loads(face_fix_snapshot('m', '3:4'))
    assert 'retouch' not in snapshot and 'resolution' not in snapshot
    assert face_fix_prompt(snapshot) == FACE_FIX_PROMPT


@pytest.mark.parametrize('mode', ['ai', 'split'])
def test_a_failed_fix_keeps_the_original(app_factory, mode):
    generator = ScriptedGenerator([None, GeneratorError('rejected')])
    with app_factory(generator=generator, face_swapper=ScriptedFaceSwapper(), auto_face_fix=mode, concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        wait_batch(client, batch['id'], status='needs_attention')
        assert fix_task(client, batch['id'])['error_code'] == 'rejected'
        assert not any(task['regenerate_mode'] == 'face_swap' for task in client.get('/api/tasks', params={'batch_id': batch['id']}).json())
        assert client.get(f"/api/projects/{project['id']}").json()['previews'][0]['current_version']['version_number'] == 1


def test_the_setting_picks_the_ai_fix_the_swap_or_nothing(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_fix='off', concurrency=1) as (client, _app):
        assert client.get('/api/settings').json()['auto_face_fix'] == 'off'
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        wait_batch(client, batch['id'])
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 1
        assert client.patch('/api/settings', json={'auto_face_fix': 'facefusion'}).json()['auto_face_fix'] == 'facefusion'
        batch = preview_batch(client, project['id'], request_id='swap', style_ids=['city_story'])
        wait_batch(client, batch['id'])
        modes = {task['regenerate_mode'] for task in client.get('/api/tasks', params={'batch_id': batch['id']}).json()}
        assert modes == {None, 'face_swap'} and swapper.calls
        assert client.patch('/api/settings', json={'auto_face_fix': 'sometimes'}).status_code == 422


def test_a_picture_can_be_fixed_by_hand_and_a_fix_starts_from_the_original(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
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


def test_the_face_model_setting_picks_the_model_of_automatic_and_manual_face_fixes(app_factory):
    """2026-11-13: the face fix has its own model ('face'; V7.32–2026-11-12 it used the wash's); the wash's and the
    hand fix's ('face_fix') settings are left to them."""
    generator = ScriptedGenerator()
    with app_factory(generator=generator, auto_face_fix='ai', concurrency=1) as (client, app):
        settings = client.patch('/api/settings', json={'face_model': 'gemini-nano-banana-2.1', 'wash_model': 'gpt-image-2.6',
                                                       'face_fix_model': 'gemini-3-pro-image'}).json()
        assert settings['face_model'] == 'gemini-nano-banana-2.1' and settings['default_model'] == 'gpt-image-2'
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        wait_batch(client, batch['id'])
        # The shot keeps the default model; only its automatic fix uses the face model.
        assert generator.models == ['gpt-image-2', 'gemini-nano-banana-2.1']
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        response = client.post(f"/api/slots/{slot['id']}/face-fix", json={'client_request_id': 'by-hand'})
        wait_batch(client, response.json()['id'])
        chosen = client.post(f"/api/slots/{slot['id']}/face-fix", json={'client_request_id': 'chosen', 'model': 'gpt-image-2.5-flare'})
        wait_batch(client, chosen.json()['id'])
        assert generator.models[2:] == ['gemini-nano-banana-2.1', 'gpt-image-2.5-flare']
        assert client.patch('/api/settings', json={'face_fix_model': 'nope'}).status_code == 422
        # PPTOKEN does not offer the Gemini model, so face fixes follow the default model again.
        assert client.patch('/api/settings', json={'image_provider': 'pptoken'}).json()['face_fix_model'] is None
        assert client.patch('/api/settings', json={'face_fix_model': 'gemini-nano-banana-2.1'}).status_code == 422
        assert client.patch('/api/settings', json={'face_fix_model': 'gpt-image-2.5'}).json()['face_fix_model'] == 'gpt-image-2.5'
        assert client.patch('/api/settings', json={'face_fix_model': None}).json()['face_fix_model'] is None


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
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
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
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
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
    section = re.search(r'### 9\.4 只修新娘(.*?)(\n### 9\.5|\n## 10\.|\Z)', doc, re.S).group(1)
    assert re.findall(r'```text\n(.*?)\n```', section, re.S) == [FACE_FIX_BRIDE_MAKEUP_PROMPT, FACE_FIX_BRIDE_PROMPT]


def test_split_fixes_the_bride_by_ai_then_swaps_only_the_groom_with_facefusion(app_factory):
    generator, swapper = ScriptedGenerator(), ScoredSwapper([])
    with app_factory(generator=generator, face_swapper=swapper, auto_face_fix='split', concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        wait_until(lambda: client.get(f"/api/batches/{batch['id']}").json(),
                   lambda batch: batch['total'] == 3 and batch['status'] == 'completed')
        modes = [task['regenerate_mode'] for task in sorted(client.get('/api/tasks', params={'batch_id': batch['id']}).json(),
                                                            key=lambda task: task['created_at'])]
        assert modes == [None, 'face_fix', 'face_swap']
        # The AI edit gets the picture and the bride's reference only, with the bride-only prompt.
        images, prompt, _at = generator.calls[1]
        assert prompt == (FACE_FIX_BRIDE_PROMPT) and len(images) == 2
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
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
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

    def generate_image(self, images, prompt, model, aspect_ratio, **_):
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
            batch = preview_batch(client, project['id'], style_ids=['city_story'])
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
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
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
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        assert wait_batch(client, batch['id'])['total'] == 2
        images, prompt, _at = generator.calls[-1]
        task = fix_task(client, batch['id'])
        with app.state.db.connect() as conn:
            references = json.loads(conn.execute('SELECT reference_image_ids FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()[0])
        assert len(images) == 2 and images[1].stem == references[0] and prompt == (FACE_FIX_BRIDE_PROMPT)
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
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        assert wait_batch(client, batch['id'])['total'] == 3
        assert len(generator.calls) == 2 and swapper.roles == [('groom',)]


def test_retrying_a_failed_groom_swap_does_not_repeat_the_paid_bride_fix(app_factory):
    generator, swapper = ScriptedGenerator(), ScriptedFaceSwapper([FaceSwapError('face_not_found')])
    with app_factory(generator=generator, face_swapper=swapper, auto_face_fix='split', concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
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
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        wait_batch(client, batch['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        original = next(version for version in client.get(f"/api/slots/{slot['id']}/versions").json() if version['version_number'] == 1)
        for action in ('face-fix', 'face-swap'):
            response = client.post(f"/api/slots/{slot['id']}/{action}", json={'client_request_id': action})
            assert response.status_code == 201, response.text
            wait_batch(client, response.json()['id'])
            task = client.get('/api/tasks', params={'batch_id': response.json()['id']}).json()[0]
            assert task['source_version_id'] == original['id']
        assert generator.calls[-1][1] == (FACE_FIX_PROMPT)
        assert swapper.roles[-1] == ('bride', 'groom')
        assert swapper.calls[-1][0].front.stem == slot['styled_asset_ids']['bride']


def test_the_hand_fix_prompt_only_touches_the_hands_and_matches_the_docs():
    assert HAND_FIX_PROMPT.startswith('这是一张已经拍好的婚纱照。只修正画面里两个人的手和手指')
    assert '五根手指' in HAND_FIX_PROMPT and '造型照' not in HAND_FIX_PROMPT
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
    section = re.search(r'## 10\. 修手(.*?)(\n## |\Z)', doc, re.S).group(1)
    # V7.79: section 10.3 adds the crop edit's prompt and the editor's hand instruction.
    from app.regional_edit import crop_edit_prompt
    blocks = re.findall(r'```text\n(.*?)\n```', section, re.S)
    assert blocks[:2] == [HAND_FIX_PROMPT, crop_edit_prompt('{用户要求}')] and len(blocks) == 3
    dialog = (Path(__file__).resolve().parents[2] / 'frontend' / 'src' / 'components' / 'RegionalEditDialog.tsx').read_text()
    assert f'const HAND_INSTRUCTION = "{blocks[2]}";' in dialog
    assert '每只手五根手指' in QUALITY_PARAGRAPH and '每只手五根手指' in ATMOSPHERE_QUALITY_PARAGRAPH


def test_a_hand_fix_edits_the_current_picture_alone_and_later_actions_start_from_the_shot(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        shot_prompt = generator.calls[-1][1]
        face = client.post(f"/api/slots/{slot['id']}/face-fix", json={'client_request_id': 'face'})
        wait_batch(client, face.json()['id'])
        face_fixed = client.get(f"/api/projects/{project['id']}").json()['previews'][0]['current_version']['id']

        hands = client.post(f"/api/slots/{slot['id']}/hand-fix", json={'client_request_id': 'hands'})
        assert hands.status_code == 201, hands.text
        wait_batch(client, hands.json()['id'])
        images, prompt, _ = generator.calls[-1]
        # Only the picture: a look would invite the edit to redraw the faces.
        assert prompt == HAND_FIX_PROMPT and len(images) == 1
        with app.state.db.connect() as conn:
            task = conn.execute('SELECT * FROM generation_tasks WHERE batch_id=?', (hands.json()['id'],)).fetchone()
            face_refs = conn.execute("SELECT reference_image_ids FROM generation_tasks WHERE regenerate_mode='face_fix'").fetchone()[0]
        # The hands are fixed on the current picture, its face fix included.
        assert task['regenerate_mode'] == 'hand_fix' and task['source_version_id'] == face_fixed
        assert json.loads(task['reference_image_ids']) == json.loads(face_refs)
        assert json.loads(task['model_config_snapshot'])['generator'] == 'hand_fix'
        current = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert current['current_version']['version_number'] == 3
        assert current['latest_task']['regenerate_mode'] == 'hand_fix'

        # A face fix after it keeps the fixed hands; a replay goes back to the shot's own prompt.
        again = client.post(f"/api/slots/{slot['id']}/face-fix", json={'client_request_id': 'face-again'})
        wait_batch(client, again.json()['id'])
        with app.state.db.connect() as conn:
            source = conn.execute('SELECT source_version_id FROM generation_tasks WHERE batch_id=?', (again.json()['id'],)).fetchone()[0]
        assert source == current['current_version']['id']
        same = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'same', 'mode': 'same'})
        wait_batch(client, same.json()['id'])
        assert generator.calls[-1][1] == shot_prompt

        bad = client.post(f"/api/slots/{slot['id']}/hand-fix", json={'client_request_id': 'bad', 'model': 'nope'})
        assert bad.status_code == 422 and bad.json()['detail']['code'] == 'invalid_model'


def test_a_hand_fix_that_makes_someone_less_alike_keeps_the_original(app_factory):
    with app_factory(generator=ScriptedGenerator(), face_swapper=FallingScores(), concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
        slot = wait_until(lambda: client.get(f"/api/projects/{project['id']}").json()['previews'][0],
                          lambda slot: slot['current_version']['face_scores'] is not None)
        response = client.post(f"/api/slots/{slot['id']}/hand-fix", json={'client_request_id': 'hands'})
        wait_batch(client, response.json()['id'])
        slot = wait_until(lambda: client.get(f"/api/projects/{project['id']}").json()['previews'][0],
                          lambda slot: slot['version_count'] == 2 and slot['current_version']['version_number'] == 1)
        with app.state.db.connect() as conn:
            row = conn.execute("SELECT model_config_snapshot FROM generation_tasks WHERE regenerate_mode='hand_fix'").fetchone()
        assert json.loads(row[0])['kept_original'] == ['groom']


def test_a_wash_that_makes_someone_less_alike_stays_current_for_the_user_to_compare(app_factory):
    """V7.79: a wash is not taken back automatically; it stays current so its compare chip shows."""
    with app_factory(generator=ScriptedGenerator(), face_swapper=FallingScores(), concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
        slot = wait_until(lambda: client.get(f"/api/projects/{project['id']}").json()['previews'][0],
                          lambda slot: slot['current_version']['face_scores'] is not None)
        original = slot['current_version']['id']
        response = client.post(f"/api/slots/{slot['id']}/wash", json={'client_request_id': 'wash'})
        wait_batch(client, response.json()['id'])
        slot = wait_until(lambda: client.get(f"/api/projects/{project['id']}").json()['previews'][0],
                          lambda slot: slot['current_version']['face_scores'] is not None
                          and slot['current_version']['version_number'] == 2)
        assert slot['current_version']['regenerate_mode'] == 'wash'
        assert slot['current_version']['source_version_id'] == original
        with app.state.db.connect() as conn:
            row = conn.execute("SELECT model_config_snapshot FROM generation_tasks WHERE regenerate_mode='wash'").fetchone()
        assert 'kept_original' not in json.loads(row[0])


def test_a_second_wash_starts_again_from_the_unwashed_picture(app_factory):
    """V7.83: washes never pile up; a wash of a washed picture washes the picture before the washes."""
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        hands = client.post(f"/api/slots/{slot['id']}/hand-fix", json={'client_request_id': 'hands'})
        wait_batch(client, hands.json()['id'])
        unwashed = client.get(f"/api/projects/{project['id']}").json()['previews'][0]['current_version']['id']
        for request_id in ('first', 'second', 'third'):
            wash = client.post(f"/api/slots/{slot['id']}/wash", json={'client_request_id': request_id})
            wait_batch(client, wash.json()['id'])
            current = client.get(f"/api/projects/{project['id']}").json()['previews'][0]['current_version']
            assert current['regenerate_mode'] == 'wash' and current['source_version_id'] == unwashed
        with app.state.db.connect() as conn:
            sources = {row[0] for row in conn.execute("SELECT source_version_id FROM generation_tasks WHERE regenerate_mode='wash'")}
        assert sources == {unwashed}


def test_a_wash_is_one_4k_edit_of_the_current_picture_with_its_own_model(app_factory):
    class SizedGenerator(ScriptedGenerator):
        def __init__(self):
            super().__init__()
            self.resolutions = []

        def generate_image(self, images, prompt, model, aspect_ratio, resolution=None, provider=None):
            self.resolutions.append(resolution)
            return super().generate_image(images, prompt, model, aspect_ratio)

    generator = SizedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        assert client.patch('/api/settings', json={'wash_model': 'gpt-image-2.5-flare'}).json()['wash_model'] == 'gpt-image-2.5-flare'
        assert client.patch('/api/settings', json={'wash_model': 'nope'}).status_code == 422
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        hands = client.post(f"/api/slots/{slot['id']}/hand-fix", json={'client_request_id': 'hands'})
        wait_batch(client, hands.json()['id'])
        hand_fixed = client.get(f"/api/projects/{project['id']}").json()['previews'][0]['current_version']['id']

        wash = client.post(f"/api/slots/{slot['id']}/wash", json={'client_request_id': 'wash'})
        assert wash.status_code == 201, wash.text
        wait_batch(client, wash.json()['id'])
        images, prompt, _ = generator.calls[-1]
        # V7.85: the same repair text for every picture, kept on the task.
        assert prompt == WASH_REPAIR_PROMPT and len(images) == 1
        # Only the wash asks for 4K, with the wash model; the shot and the hand fix use the configured size.
        assert generator.resolutions == [None, None, '4K']
        assert generator.models[-1] == 'gpt-image-2.5-flare'
        with app.state.db.connect() as conn:
            task = conn.execute('SELECT * FROM generation_tasks WHERE batch_id=?', (wash.json()['id'],)).fetchone()
        assert task['regenerate_mode'] == 'wash' and task['source_version_id'] == hand_fixed and task['prompt'] == prompt
        assert json.loads(task['model_config_snapshot'])['resolution'] == '4K'
        current = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert current['current_version']['regenerate_mode'] == 'wash'
        chosen = client.post(f"/api/slots/{slot['id']}/wash", json={'client_request_id': 'chosen', 'model': 'gpt-image-2'})
        wait_batch(client, chosen.json()['id'])
        assert generator.models[-1] == 'gpt-image-2'


def test_the_wash_prompt_matches_the_docs():
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
    section = re.search(r'## 11\. 洗图(.*?)(\n## |\Z)', doc, re.S).group(1)
    blocks = re.findall(r'```text\n(.*?)\n```', section, re.S)
    # The current text first, then V7.79's (kept for the record) and V7.26's (still used by older tasks).
    assert len(blocks) == 3 and blocks[0] == WASH_REPAIR_PROMPT and blocks[2] == WASH_PROMPT
    assert blocks[1].startswith('以这张婚纱照为唯一主体参考') and '{风格的视觉语言}' in blocks[1]


def test_hand_fixes_and_washes_each_go_to_their_own_service(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, auto_face_fix='ai', concurrency=1) as (client, app):
        settings = client.get('/api/settings').json()
        assert (settings['image_provider'], settings['face_fix_provider'], settings['wash_provider']) == ('88api', None, None)
        # PPTOKEN's own model for the face fix; the wash stays on 88API with a model only 88API offers.
        assert client.patch('/api/settings', json={'face_fix_model': 'gpt-image-2.5'}).status_code == 422
        settings = client.patch('/api/settings', json={'face_fix_provider': 'pptoken', 'face_fix_model': 'gpt-image-2.5',
                                                       'wash_model': 'gemini-3-pro-image', 'face_model': 'gemini-3-pro-image'}).json()
        assert (settings['face_fix_provider'], settings['face_fix_model'], settings['wash_model']) == ('pptoken', 'gpt-image-2.5', 'gemini-3-pro-image')
        assert [m['id'] for m in client.get('/api/models', params={'provider': 'pptoken'}).json()] == ['gpt-image-2', 'gpt-image-2.5', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst', 'gpt-image-2.6', 'gpt-image-2.6-sunburst', 'gpt-image-2.6-flare']
        assert client.get('/api/settings').json()['image_provider'] == '88api'

        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
        # The shot follows the image service; its automatic face fix follows the face setting, on 88API.
        assert generator.models[-2:] == ['gpt-image-2', 'gemini-3-pro-image'] and generator.providers[-2:] == [None, '88api']
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        hands = client.post(f"/api/slots/{slot['id']}/hand-fix", json={'client_request_id': 'hands'})
        wait_batch(client, hands.json()['id'])
        assert (generator.models[-1], generator.providers[-1]) == ('gpt-image-2.5', 'pptoken')
        # A model the face fix's service does not offer is refused.
        assert client.post(f"/api/slots/{slot['id']}/face-fix",
                           json={'client_request_id': 'pptoken-only', 'model': 'gpt-image-2.5'}).status_code == 422
        wash = client.post(f"/api/slots/{slot['id']}/wash", json={'client_request_id': 'wash'})
        wait_batch(client, wash.json()['id'])
        assert (generator.models[-1], generator.providers[-1]) == ('gemini-3-pro-image', '88api')

        # Moving the wash to PPTOKEN drops its 88API-only model: it follows the default model, which PPTOKEN offers.
        settings = client.patch('/api/settings', json={'wash_provider': 'pptoken'}).json()
        assert (settings['wash_provider'], settings['wash_model']) == ('pptoken', None)
        assert app.state.db.get_edit_choice('wash') == ('pptoken', 'gpt-image-2')
        # Back to following the image service.
        settings = client.patch('/api/settings', json={'face_fix_provider': None}).json()
        assert (settings['face_fix_provider'], settings['face_fix_model']) == (None, None)
        assert app.state.db.get_edit_choice('face_fix') == ('88api', 'gpt-image-2')


def _modes(client, batch_id):
    tasks = sorted(client.get('/api/tasks', params={'batch_id': batch_id}).json(), key=lambda task: task['created_at'])
    return [task['regenerate_mode'] for task in tasks]


@pytest.mark.parametrize(('bride', 'groom', 'modes', 'prompt', 'swapped'), [
    # Each person on their own (V7.29): AI for one, nothing for the other.
    ('off', 'ai', [None, 'face_fix'], 'groom', []),
    ('ai', 'off', [None, 'face_fix'], 'bride', []),
    # The AI fix first, FaceFusion after it, either way round.
    ('facefusion', 'ai', [None, 'face_fix', 'face_swap'], 'groom', [('bride',)]),
    # FaceFusion for one person only.
    ('facefusion', 'off', [None, 'face_swap'], None, [('bride',)]),
    ('off', 'facefusion', [None, 'face_swap'], None, [('groom',)]),
])
def test_each_person_gets_their_own_automatic_fix(app_factory, bride, groom, modes, prompt, swapped):
    generator, swapper = ScriptedGenerator(), ScriptedFaceSwapper()
    with app_factory(generator=generator, face_swapper=swapper, concurrency=1) as (client, app):
        settings = client.patch('/api/settings', json={'auto_face_fix_bride': bride, 'auto_face_fix_groom': groom}).json()
        assert (settings['auto_face_fix_bride'], settings['auto_face_fix_groom'], settings['auto_face_fix']) == (bride, groom, 'custom')
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['city_story'])
        wait_until(lambda: client.get(f"/api/batches/{batch['id']}").json(),
                   lambda batch: batch['total'] == len(modes) and batch['status'] == 'completed')
        assert _modes(client, batch['id']) == modes
        assert swapper.roles == swapped
        if prompt:
            images, sent, _at = generator.calls[1]
            slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
            # Only the fixed person's look goes with the picture.
            assert sent == (FACE_FIX_GROOM_PROMPT if prompt == 'groom' else FACE_FIX_BRIDE_PROMPT)
            assert [path.stem for path in images[1:]] == [slot['styled_asset_ids'][prompt]]
        else:
            assert len(generator.calls) == 1


def test_the_presets_set_both_people_and_an_old_preset_still_reads(tmp_path):
    from app.db import Database
    db = Database(tmp_path / 'test.db')
    db.initialize()
    assert db.get_auto_face_fix_roles() == {'bride': 'ai', 'groom': 'facefusion'} and db.get_auto_face_fix() == 'split'
    # A database from before V7.29 holds only the preset.
    with db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings (key,value) VALUES ('auto_face_fix','ai')")
    assert db.get_auto_face_fix_roles() == {'bride': 'ai', 'groom': 'ai'} and db.get_auto_face_fix() == 'ai'
    db.set_auto_face_fix_roles(groom='off')
    assert db.get_auto_face_fix_roles() == {'bride': 'ai', 'groom': 'off'} and db.get_auto_face_fix() == 'custom'
    db.set_auto_face_fix('facefusion')
    assert db.get_auto_face_fix_roles() == {'bride': 'facefusion', 'groom': 'facefusion'}


def test_the_groom_only_prompt_leaves_the_bride_and_matches_the_docs():
    assert '请只校正照片里新郎的长相' in FACE_FIX_GROOM_PROMPT and '新娘不在这次修正范围内' in FACE_FIX_GROOM_PROMPT
    assert '新娘的脸、妆容、发型、肤色、表情和全身' in FACE_FIX_GROOM_PROMPT and '第三张图' not in FACE_FIX_GROOM_PROMPT
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
    section = re.search(r'### 9\.5 只修新郎(.*?)(\n### |\n## |\Z)', doc, re.S).group(1)
    assert re.findall(r'```text\n(.*?)\n```', section, re.S) == [FACE_FIX_GROOM_PROMPT]


def test_the_face_fix_starts_with_the_wash_settings_it_used_before_they_were_split(tmp_path):
    """2026-11-13: an installation upgraded from V7.32–2026-11-12 keeps the face fix on the wash's service and model."""
    from app.db import Database
    db = Database(tmp_path / 'vowframe.db')
    db.initialize()
    with db.connect(write=True) as conn:
        conn.execute("DELETE FROM app_settings WHERE key IN ('face_model','face_provider')")
    db.set_stage_model('wash', 'gpt-image-2.6')
    db.set_edit_provider('wash', 'pptoken')
    db.initialize()
    assert (db.get_stage_model('face'), db.get_edit_provider('face')) == ('gpt-image-2.6', 'pptoken')
    db.set_stage_model('face', None)
    db.initialize()  # seeded once: a later choice is kept
    assert db.get_stage_model('face') is None
