"""Regional edits preserve annotation semantics, source versions and billing boundaries."""

import base64
import io
import json
import sqlite3
import threading

import httpx
import pytest
from PIL import Image, ImageDraw

from app.api88 import Api88Generator
from app.config import Config
from app.db import Database, SCHEMA, new_id
from app.generator import GeneratorError, ProviderGenerator
from app.regional_edit import RegionalEdit, oriented_size, png_result, validate_annotation
from app.storage import asset_path
from conftest import PNG, ScriptedGenerator, create_project, preview_batch, wait_batch
from test_api88 import multipart_parts

SIZE = (256, 384)


def annotation(kind='markup', size=SIZE):
    image = Image.new('RGBA', size, (255, 255, 255, 255) if kind == 'mask' else (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if kind == 'mask':
        draw.ellipse((40, 60, 120, 170), fill=(0, 0, 0, 0))
    else:
        draw.ellipse((40, 60, 120, 170), outline='red', width=4)
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


class RegionalGenerator(ScriptedGenerator):
    def __init__(self):
        super().__init__()
        self.edits = []
        self.block = False
        self.started, self.release = threading.Event(), threading.Event()

    def generate_image(self, images, prompt, model, aspect_ratio, resolution=None, provider=None, edit=None):
        if edit is not None:
            self.edits.append((edit, edit.annotation.read_bytes()))
            if self.block:
                self.started.set()
                assert self.release.wait(10), 'Regional edit was not released'
        return super().generate_image(images, prompt, model, aspect_ratio, resolution, provider)


def ready(client, app):
    project = create_project(client)
    wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
    slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
    with app.state.db.connect() as conn:
        relative = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (slot['current_version']['asset_id'],)).fetchone()[0]
    Image.new('RGB', SIZE, '#eee6df').save(asset_path(app.state.config, relative), 'PNG')
    return project, slot


def submit(client, slot, request_id='regional', kind='markup', **overrides):
    data = {'client_request_id': request_id, 'source_version_id': slot['current_version']['id'],
            'prompt': '修正圈内粘连的手指，保持戒指和手势', 'model': 'gpt-image-2.6-flare', 'provider': '88api'}
    data.update(overrides)
    return client.post(f"/api/slots/{slot['id']}/regional-edit", data=data,
                       files={kind: (kind + '.png', annotation(kind), 'image/png')})


@pytest.mark.parametrize('kind', ['reference', 'markup', 'mask'])
def test_an_edit_uses_one_immutable_source_and_keeps_a_restorable_version(app_factory, kind):
    generator = RegionalGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project, slot = ready(client, app)
        # Enabling automatic face fixes must not turn a local edit into another whole-image edit.
        client.patch('/api/settings', json={'auto_face_fix': 'ai'})
        response = submit(client, slot, kind='markup' if kind == 'reference' else kind,
                          method='reference' if kind == 'reference' else 'native')
        assert response.status_code == 201, response.text
        batch = wait_batch(client, response.json()['id'])
        assert batch['total'] == 1
        task = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
        assert task['regenerate_mode'] == 'regional_edit' and task['prompt'].startswith('修正圈内')
        assert task['source_version_id'] == slot['current_version']['id']
        assert len(generator.edits) == 1 and generator.edits[0][0].kind == kind
        assert len(generator.calls[-1][0]) == 1 and '只完成标记区域内' in generator.calls[-1][1]
        assert generator.providers[-1] == '88api'
        with app.state.db.connect() as conn:
            snapshot = json.loads(conn.execute('SELECT model_config_snapshot FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()[0])
        assert snapshot['size'] == 'auto' and snapshot['quality'] == 'medium' and snapshot['output_format'] == 'png'
        assert snapshot['source_size'] == list(SIZE)
        versions = client.get(f"/api/slots/{slot['id']}/versions").json()
        current = next(v for v in versions if v['is_current'])
        assert len(versions) == 2 and current['regenerate_mode'] == 'regional_edit'
        assert current['source_version_id'] == slot['current_version']['id'] and current['size_warning'] is None
        detail = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert detail['look_ids'] == slot['look_ids']
        restored = client.post(f"/api/slots/{slot['id']}/restore", json={'version_id': slot['current_version']['id']})
        assert restored.status_code == 200 and restored.json()['current_version']['id'] == slot['current_version']['id']


def test_repeated_submission_is_idempotent_even_after_the_current_version_changes(app_factory):
    generator = RegionalGenerator()
    with app_factory(generator=generator) as (client, app):
        _, slot = ready(client, app)
        first = submit(client, slot)
        wait_batch(client, first.json()['id'])
        again = submit(client, slot)
        assert again.status_code == 200 and again.json()['id'] == first.json()['id']
        assert len(generator.edits) == 1
        assert submit(client, slot, prompt='另一个修改').status_code == 409


@pytest.mark.parametrize('overrides,code', [
    ({'prompt': '  '}, 'invalid_prompt'), ({'model': 'gpt-image-2'}, 'invalid_model'),
    ({'model': 'gemini-3-pro-image'}, 'invalid_model'), ({'model': 'gpt-image-2.5'}, 'invalid_model'),
    ({'source_version_id': 'older-version'}, 'source_changed'),
])
def test_invalid_requests_do_not_create_an_asset_or_call_the_provider(app_factory, overrides, code):
    generator = RegionalGenerator()
    with app_factory(generator=generator) as (client, app):
        _, slot = ready(client, app)
        with app.state.db.connect() as conn:
            before = conn.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0]
        response = submit(client, slot, **overrides)
        assert response.status_code in (409, 422) and response.json()['detail']['code'] == code
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0] == before
        assert not generator.edits


def test_a_failed_edit_can_retry_the_same_annotation_and_keeps_the_original(app_factory):
    generator = RegionalGenerator()
    with app_factory(generator=generator) as (client, app):
        project, slot = ready(client, app)
        generator.outcomes.append(GeneratorError('rejected'))
        response = submit(client, slot)
        wait_batch(client, response.json()['id'], status='needs_attention')
        task = client.get('/api/tasks', params={'batch_id': response.json()['id']}).json()[0]
        assert len(generator.edits) == 1
        current = client.get(f"/api/projects/{project['id']}").json()['previews'][0]['current_version']
        assert current['id'] == slot['current_version']['id']
        client.patch('/api/settings', json={'default_model': 'gpt-image-2', 'image_provider': 'pptoken'})
        retry = client.post(f"/api/tasks/{task['id']}/retry")
        assert retry.status_code == 201
        wait_batch(client, response.json()['id'])
        assert generator.edits[0] == generator.edits[1] and generator.models[-1] == 'gpt-image-2.6-flare'
        assert generator.providers[-1] == '88api'


def test_restoring_a_version_while_an_edit_runs_is_not_overridden(app_factory):
    generator = RegionalGenerator()
    try:
        with app_factory(generator=generator, concurrency=1) as (client, app):
            project, slot = ready(client, app)
            wait_batch(client, submit(client, slot).json()['id'])
            newer = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
            generator.block = True
            response = submit(client, newer, request_id='late-result')
            assert generator.started.wait(3)
            assert submit(client, newer, request_id='busy').json()['detail']['code'] == 'slot_busy'
            client.post(f"/api/slots/{slot['id']}/restore", json={'version_id': slot['current_version']['id']})
            generator.release.set()
            wait_batch(client, response.json()['id'])
            versions = client.get(f"/api/slots/{slot['id']}/versions").json()
            assert len(versions) == 3
            assert next(v['id'] for v in versions if v['is_current']) == slot['current_version']['id']
    finally:
        generator.release.set()


def test_historical_held_task_is_explained_and_can_be_cancelled_before_editing(app_factory):
    generator = RegionalGenerator()
    with app_factory(generator=generator) as (client, app):
        project, slot = ready(client, app)
        other = preview_batch(client, project['id'], request_id='other-style', style_ids=['forest_film'])
        wait_batch(client, other['id'])
        with app.state.db.connect(write=True) as conn:
            held = dict(conn.execute('SELECT * FROM generation_tasks WHERE id=?', (slot['latest_task']['id'],)).fetchone())
            held.update(id=new_id(), status='held', created_at='2000-01-01T00:00:00+00:00',
                        started_at=None, finished_at=None, output_asset_id=None)
            columns = list(held)
            conn.execute('INSERT INTO generation_tasks (' + ','.join(columns) + ') VALUES ('
                         + ','.join('?' for _ in columns) + ')', [held[key] for key in columns])
            assets_before = conn.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0]
        detail = client.get(f"/api/projects/{project['id']}").json()
        current = next(item for item in detail['previews'] if item['id'] == slot['id'])
        assert current['latest_task']['status'] == 'completed'
        tasks = client.get('/api/tasks', params={'target_slot_id': slot['id']}).json()
        assert {task['id'] for task in tasks} == {slot['latest_task']['id'], held['id']}
        filtered = client.get('/api/tasks', params={'target_slot_id': slot['id'], 'status': 'held'}).json()
        assert [task['id'] for task in filtered] == [held['id']]
        blocked = submit(client, slot)
        assert blocked.status_code == 409 and blocked.json()['detail']['code'] == 'slot_busy'
        assert '已暂停' in blocked.json()['detail']['message'] and '不会自动完成' in blocked.json()['detail']['message']
        assert not generator.edits
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0] == assets_before
        cancelled = client.post(f"/api/tasks/{held['id']}/cancel", params={'expected_status': 'held'})
        assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled'
        detail = client.get(f"/api/projects/{project['id']}").json()
        current = next(item for item in detail['previews'] if item['id'] == slot['id'])
        assert current['current_version']['id'] == slot['current_version']['id']
        accepted = submit(client, slot)
        assert accepted.status_code == 201, accepted.text
        wait_batch(client, accepted.json()['id'])
        assert len(generator.edits) == 1


def test_cancelling_a_paused_task_never_stops_a_request_that_has_started(app_factory):
    generator = RegionalGenerator()
    try:
        with app_factory(generator=generator, concurrency=1) as (client, app):
            _, slot = ready(client, app)
            generator.block = True
            response = submit(client, slot)
            assert generator.started.wait(3)
            task = client.get('/api/tasks', params={'batch_id': response.json()['id']}).json()[0]
            refused = client.post(f"/api/tasks/{task['id']}/cancel", params={'expected_status': 'held'})
            assert refused.status_code == 409 and refused.json()['detail']['code'] == 'task_state_changed'
            latest = client.get('/api/tasks', params={'target_slot_id': slot['id'], 'status': 'running'}).json()
            assert [item['id'] for item in latest] == [task['id']]
            generator.release.set()
            wait_batch(client, response.json()['id'])
    finally:
        generator.release.set()


@pytest.mark.parametrize('kind', ['reference', 'markup', 'mask'])
def test_relay_receives_original_and_separate_annotation_with_edit_only_parameters(tmp_path, kind):
    source, overlay = tmp_path / 'source.png', tmp_path / 'annotation.png'
    Image.new('RGB', SIZE, 'white').save(source)
    overlay.write_bytes(annotation(kind))
    calls = []

    def handler(request):
        calls.append(request)
        parts = multipart_parts(request)
        fields = {name: value.decode() for name, filename, _, value in parts if filename is None}
        assert fields == {'model': 'gpt-image-2.6-flare', 'prompt': 'instruction', 'size': 'auto',
                          'quality': 'medium', 'output_format': 'png', 'n': '1'}
        files = [(name, mime, value) for name, filename, mime, value in parts if filename is not None]
        assert [name for name, _, _ in files] == (['image[]', 'image[]'] if kind == 'reference' else ['image', kind])
        assert files[1][1] == 'image/png'
        with Image.open(io.BytesIO(files[1][2])) as image:
            assert image.size == SIZE
            if kind == 'reference':
                assert image.mode == 'RGB'
                assert image.getpixel((80, 60))[0] > 200 and image.getpixel((80, 60))[1] < 40
                with Image.open(io.BytesIO(files[0][2])) as original:
                    assert original.getpixel((80, 60)) == (255, 255, 255)
            else:
                assert image.getchannel('A').getextrema() == (0, 255)
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(PNG).decode()}]})

    client = Api88Generator(Config(tmp_path, 1, '88api', 0, api_key='test-key'), transport=httpx.MockTransport(handler))
    router = ProviderGenerator({'88api': client}, lambda: '88api')
    assert router.generate_image([source], 'instruction', 'gpt-image-2.6-flare', '3:4', provider='88api',
                                 edit=RegionalEdit(overlay, kind)) == PNG
    assert len(calls) == 1


def test_known_unsupported_markup_is_disabled_and_reference_remains_explicit(app_factory):
    generator = RegionalGenerator()
    with app_factory(generator=generator) as (client, app):
        _, slot = ready(client, app)
        options = client.get('/api/regional-edit/options?provider=pptoken').json()
        assert not next(m for m in options['methods'] if m['id'] == 'markup')['available']
        rejected = submit(client, slot, provider='pptoken')
        assert rejected.status_code == 422 and rejected.json()['detail']['code'] == 'markup_unsupported'
        assert not generator.edits
        accepted = submit(client, slot, provider='pptoken', method='reference')
        assert accepted.status_code == 201
        wait_batch(client, accepted.json()['id'])
        assert generator.edits[0][0].kind == 'reference'


def test_both_annotation_fields_and_missing_annotations_are_rejected(app_factory):
    with app_factory() as (client, app):
        _, slot = ready(client, app)
        data = {'client_request_id': 'invalid', 'source_version_id': slot['current_version']['id'],
                'prompt': 'edit', 'model': 'gpt-image-2.6-flare', 'provider': '88api'}
        for files in ({}, {'markup': ('m.png', annotation(), 'image/png'), 'mask': ('k.png', annotation('mask'), 'image/png')}):
            response = client.post(f"/api/slots/{slot['id']}/regional-edit", data=data, files=files)
            assert response.status_code == 422 and response.json()['detail']['code'] == 'invalid_annotation'


def test_rejected_annotation_is_never_silently_removed_or_resubmitted(tmp_path):
    source, overlay = tmp_path / 'source.png', tmp_path / 'annotation.png'
    Image.new('RGB', SIZE).save(source)
    overlay.write_bytes(annotation())
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={'error': {'message': 'Unknown parameter markup'}})

    client = Api88Generator(Config(tmp_path, 1, '88api', 0, api_key='test-key'), transport=httpx.MockTransport(handler))
    with pytest.raises(GeneratorError, match='局部编辑请求被拒绝') as caught:
        client.generate_image([source], 'p', 'gpt-image-2.6-flare', '3:4', edit=RegionalEdit(overlay, 'markup'))
    assert caught.value.error_code == 'regional_edit_rejected' and len(calls) == 1


def test_annotation_validation_rejects_empty_opaque_wrong_size_and_invalid_files():
    for mode, color, size in [('RGB', 'red', SIZE), ('RGBA', (0, 0, 0, 0), SIZE),
                              ('RGBA', 'red', SIZE), ('RGBA', 'red', (10, 10))]:
        output = io.BytesIO()
        Image.new(mode, size, color).save(output, 'PNG')
        with pytest.raises(ValueError):
            validate_annotation(output.getvalue(), SIZE, 'markup')
    with pytest.raises(ValueError):
        validate_annotation(b'not an image', SIZE, 'mask')
    with pytest.raises(ValueError):
        validate_annotation(annotation(), SIZE, 'other')


def test_annotation_alignment_uses_the_oriented_source_dimensions(tmp_path):
    source = tmp_path / 'oriented.jpg'
    image = Image.new('RGB', (384, 256))
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, exif=exif)
    assert oriented_size(source) == SIZE
    assert validate_annotation(annotation(), oriented_size(source), 'markup')


def test_edit_results_are_validated_and_saved_as_png():
    source = io.BytesIO()
    Image.new('RGB', SIZE, '#2244bb').save(source, 'JPEG')
    with Image.open(io.BytesIO(png_result(source.getvalue()))) as result:
        assert result.format == 'PNG' and result.size == SIZE
    with pytest.raises(ValueError, match='可能已经计费'):
        png_result(b'broken output')


def test_database_upgrade_preserves_versions_retry_links_and_foreign_keys(tmp_path):
    path = tmp_path / 'old.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA.replace(",'regional_edit'", ''))
        conn.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','p','now','now')")
        conn.execute("INSERT INTO style_presets VALUES ('city_story','s','s','s',1)")
        conn.execute("INSERT INTO generation_batches (id,project_id,batch_type,client_request_id,created_at) VALUES ('b','p','preview','r','now')")
        conn.execute("INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,created_at) VALUES ('s','p','b','preview','city_story','now')")
        conn.execute("INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,prompt,model_config_snapshot,created_at) "
                     "VALUES ('t','p','b','s','completed','[]','city_story','p','{}','now')")
        conn.execute("INSERT INTO image_assets VALUES ('a','p','a.png','preview','now')")
        conn.execute("INSERT INTO photo_versions (id,photo_slot_id,image_asset_id,generation_task_id,version_number,created_at) VALUES ('v','s','a','t',1,'now')")
    db = Database(path)
    db.initialize()
    db.initialize()
    with db.connect(write=True) as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('SELECT generation_task_id FROM photo_versions WHERE id=\'v\'').fetchone()[0] == 't'
        conn.execute("UPDATE generation_tasks SET regenerate_mode='regional_edit',source_version_id='v' WHERE id='t'")


def crop_annotation(size=SIZE, box=(40, 60, 120, 170)):
    """A crop mark: the area to change painted over (filled), as the editor draws it."""
    image = Image.new('RGBA', size, (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse(box, fill=(239, 52, 52, 255))
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


class CropGenerator(RegionalGenerator):
    """Answers a crop edit with a solid blue square, so the laid-back area is easy to find."""

    def __init__(self):
        super().__init__()
        self.inputs = []

    def generate_image(self, images, prompt, model, aspect_ratio, resolution=None, provider=None, edit=None):
        if edit is None and len(images) == 2 and aspect_ratio == '1:1':
            self.inputs.append(([Image.open(path).copy() for path in images], resolution, prompt))
            super().generate_image(images, prompt, model, aspect_ratio, resolution, provider)
            output = io.BytesIO()
            Image.new('RGB', (1024, 1024), (0, 0, 255)).save(output, 'PNG')
            return output.getvalue()
        return super().generate_image(images, prompt, model, aspect_ratio, resolution, provider, edit)


def test_a_crop_edit_enlarges_the_marked_area_and_changes_nothing_else(app_factory):
    """V7.79: the marked area is edited enlarged and only it is laid back; the picture keeps its size and pixels."""
    generator = CropGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project, slot = ready(client, app)
        response = client.post(f"/api/slots/{slot['id']}/regional-edit", data={
            'client_request_id': 'crop', 'source_version_id': slot['current_version']['id'], 'prompt': '修正手指',
            'model': 'gpt-image-2.6-flare', 'provider': '88api', 'method': 'crop',
        }, files={'markup': ('crop.png', crop_annotation(), 'image/png')})
        assert response.status_code == 201, response.text
        batch = wait_batch(client, response.json()['id'])
        assert batch['status'] == 'completed' and not generator.edits
        (clean, marked), resolution, prompt = generator.inputs[0]
        assert clean.size == marked.size == (1024, 1024) and resolution == '1K' and '用户要求：修正手指' in prompt
        assert clean.getpixel((512, 512)) == (238, 230, 223)            # the clean crop is the picture
        # The picture is narrower than the minimum crop, so the crop is its full 256 px width: (80, 115) -> (320, 460).
        assert marked.getpixel((320, 460))[2] < 200 and marked.getpixel((900, 900)) == clean.getpixel((900, 900))
        task = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
        with app.state.db.connect() as conn:
            snapshot = json.loads(conn.execute('SELECT model_config_snapshot FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()[0])
            output = conn.execute('SELECT output_asset_id FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()[0]
            relative = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (output,)).fetchone()[0]
        assert snapshot['annotation_kind'] == 'crop' and snapshot['size'] == '1024x1024' and 'quality' not in snapshot
        with Image.open(asset_path(app.state.config, relative)) as result:
            result = result.convert('RGB')
            assert result.size == SIZE
            assert result.getpixel((80, 115)) == (0, 0, 255)            # inside the mark: the edit
            assert result.getpixel((200, 300)) == (238, 230, 223)       # far outside: untouched
            assert result.getpixel((5, 5)) == (238, 230, 223)


def test_crop_box_is_a_square_around_the_mark_kept_inside_the_picture():
    from app.regional_edit import crop_box
    assert crop_box((700, 800, 900, 950), (1536, 2048)) == (600, 675, 1000, 1075)    # twice the longer side
    assert crop_box((0, 0, 50, 40), (1536, 2048)) == (0, 0, 320, 320)               # small marks keep context
    assert crop_box((1400, 1900, 1530, 2040), (1536, 2048)) == (1216, 1728, 1536, 2048)
    assert crop_box((0, 0, 1536, 2048), (1536, 2048)) == (0, 256, 1536, 1792)       # never larger than the picture


def test_nano_banana_21_is_offered_for_the_crop_edit_only(app_factory):
    """2026-11-11: the crop edit sends two plain pictures, which Nano Banana 2.1 takes; the other methods need
    OpenAI edit fields or redraw the whole picture, so they stay with GPT Image."""
    generator = CropGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        _, slot = ready(client, app)
        options = client.get('/api/regional-edit/options?provider=88api').json()
        assert 'gemini-nano-banana-2.1' in [model['id'] for model in options['models']]
        assert 'gemini-3-pro-image' not in [model['id'] for model in options['models']]
        by_id = {method['id']: method['models'] for method in options['methods']}
        assert 'gemini-nano-banana-2.1' in by_id['crop'] and 'gpt-image-2.6-flare' in by_id['crop']
        for kind in ('reference', 'markup', 'mask'):
            assert 'gemini-nano-banana-2.1' not in by_id[kind] and 'gpt-image-2.6-flare' in by_id[kind]
        # PPTOKEN does not offer it.
        pptoken = client.get('/api/regional-edit/options?provider=pptoken').json()
        assert 'gemini-nano-banana-2.1' not in [model['id'] for model in pptoken['models']]

        sent = len(generator.calls)
        for overrides in ({'method': 'reference'}, {'method': 'native'}):
            rejected = submit(client, slot, request_id='g-' + overrides['method'], model='gemini-nano-banana-2.1', **overrides)
            assert rejected.status_code == 422 and rejected.json()['detail']['code'] == 'method_unsupported'
        assert len(generator.calls) == sent

        response = client.post(f"/api/slots/{slot['id']}/regional-edit", data={
            'client_request_id': 'gemini-crop', 'source_version_id': slot['current_version']['id'], 'prompt': '修正手指',
            'model': 'gemini-nano-banana-2.1', 'provider': '88api', 'method': 'crop',
        }, files={'markup': ('crop.png', crop_annotation(), 'image/png')})
        assert response.status_code == 201, response.text
        assert wait_batch(client, response.json()['id'])['status'] == 'completed'
        (clean, marked), resolution, _ = generator.inputs[0]
        assert clean.size == marked.size == (1024, 1024) and resolution == '1K'
        assert generator.models[-1] == 'gemini-nano-banana-2.1' and generator.providers[-1] == '88api'


def test_seedream_on_ark_is_offered_for_the_crop_edit_only(app_factory):
    """2026-11-13: the crop edit is two plain pictures, which Ark's Seedream 5.5 takes; the whole-picture methods
    need OpenAI edit fields, so they are refused before anything is sent."""
    generator = CropGenerator()
    seedream = 'doubao-seedream-5-5-261028'
    with app_factory(generator=generator, concurrency=1) as (client, app):
        _, slot = ready(client, app)
        options = client.get('/api/regional-edit/options?provider=ark').json()
        assert [model['id'] for model in options['models']] == [seedream]
        by_id = {method['id']: method['models'] for method in options['methods']}
        assert by_id['crop'] == [seedream] and all(by_id[kind] == [] for kind in ('reference', 'markup', 'mask'))

        sent = len(generator.calls)
        rejected = submit(client, slot, request_id='ark-reference', model=seedream, provider='ark', method='reference')
        assert rejected.status_code == 422 and rejected.json()['detail']['code'] == 'method_unsupported'
        assert len(generator.calls) == sent

        response = client.post(f"/api/slots/{slot['id']}/regional-edit", data={
            'client_request_id': 'ark-crop', 'source_version_id': slot['current_version']['id'], 'prompt': '修正手指',
            'model': seedream, 'provider': 'ark', 'method': 'crop',
        }, files={'markup': ('crop.png', crop_annotation(), 'image/png')})
        assert response.status_code == 201, response.text
        assert wait_batch(client, response.json()['id'])['status'] == 'completed'
        (clean, marked), resolution, _ = generator.inputs[0]
        assert clean.size == marked.size == (1024, 1024) and resolution == '1K'
        assert generator.models[-1] == seedream and generator.providers[-1] == 'ark'
