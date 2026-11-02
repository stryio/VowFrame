"""The AI face fix (V7.1; the whole picture since V7.2): one edit of a finished picture with the bride's made-up photo
and the groom's look (V7.9), kept as the slot's new version; it runs after every generated picture with faces unless the setting says otherwise."""

import json
import re
from pathlib import Path

from app.generator import GeneratorError
from app.prompts import FACE_FIX_MAKEUP_PROMPT, FACE_FIX_PROMPT
from conftest import ScriptedGenerator, create_project, preview_batch, wait_batch, wait_until
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


def test_a_failed_fix_keeps_the_original(app_factory):
    generator = ScriptedGenerator([None, GeneratorError('rejected')])
    with app_factory(generator=generator, auto_face_fix='ai', concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        assert fix_task(client, batch['id'])['error_code'] == 'rejected'
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
