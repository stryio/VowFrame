"""The AI face fix (V7.1; the whole picture since V7.2): one edit of a finished picture with both uploaded photos,
kept as the slot's new version; it runs after every generated picture with faces unless the setting says otherwise."""

import json
import re
from pathlib import Path

from app.generator import GeneratorError
from app.prompts import FACE_FIX_PROMPT
from conftest import ScriptedGenerator, create_project, preview_batch, wait_batch
from test_face_swap import ScriptedFaceSwapper


def fix_task(client, batch_id):
    return next(task for task in client.get('/api/tasks', params={'batch_id': batch_id}).json() if task['regenerate_mode'] == 'face_fix')


def test_the_prompt_assigns_each_attribute_to_its_image_and_matches_the_docs():
    assert FACE_FIX_PROMPT.startswith('第一张图是需要修正的婚纱照。第二张图是新娘本人的原始照片，第三张图是新郎本人的原始照片')
    assert '长相依据第二、三张图' in FACE_FIX_PROMPT and '妆容和精修依据第一张图' in FACE_FIX_PROMPT
    assert '不新增美白或磨皮' in FACE_FIX_PROMPT and '换脸' not in FACE_FIX_PROMPT
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
    assert re.search(r'### 9\.2 编辑提示词.*?```text\n(.*?)\n```', doc, re.S).group(1) == FACE_FIX_PROMPT


def test_every_picture_gets_one_ai_face_fix_of_the_whole_picture_without_facefusion(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, auto_face_fix='ai', concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 2
        _shot, (images, prompt, _at) = generator.calls
        assert prompt == FACE_FIX_PROMPT and generator.aspect_ratios == ['3:4', '3:4']
        uploads = [project['person_a']['asset_id'], project['person_b']['asset_id']]
        # The picture, then the bride's and the groom's uploaded photos (never the looks).
        assert [path.stem for path in images[1:]] == uploads
        task = fix_task(client, batch['id'])
        with app.state.db.connect() as conn:
            row = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
            source = conn.execute('SELECT asset.file_path FROM photo_versions version JOIN image_assets asset '
                                  'ON asset.id=version.image_asset_id WHERE version.id=?', (row['source_version_id'],)).fetchone()
        assert images[0].as_posix().endswith(source['file_path'])
        assert json.loads(row['reference_image_ids']) == uploads
        assert json.loads(row['model_config_snapshot']) == {'generator': 'face_fix', 'model': 'gpt-image-2', 'aspect_ratio': '3:4'}
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert slot['current_version']['version_number'] == 2 and slot['current_version']['source_version_id']
        assert slot['look_ids'] is not None                     # the fixed picture keeps its sample's looks


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
