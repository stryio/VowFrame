"""The AI face fix (V7.1): each person's face is edited on a crop with their uploaded photo and only the face is
laid back (app/face_fix.py); it runs after every generated picture with faces unless the setting says otherwise."""

import io
import json

import numpy as np
import pytest
from PIL import Image

from app.face_fix import (EDIT_SIDE, Face, FaceFixError, FaceFixer, crop, crop_rect, face_from_analysis, position)
from app.prompts import compose_face_fix_prompt
from conftest import ScriptedGenerator, create_project, preview_batch, uploaded_ids, wait_batch, wait_until
from test_face_swap import ScriptedFaceSwapper

# A face box and its five points in a 400 x 500 picture.
FACE = Face((150.0, 150.0, 100.0, 120.0), ((175.0, 195.0), (225.0, 195.0), (200.0, 225.0), (180.0, 250.0), (220.0, 250.0)))


def picture(color, size=(400, 500)) -> Image.Image:
    return Image.new('RGB', size, color)


def png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


def all_skin(image, box):
    return np.ones((image.height, image.width), np.float32)


def in_crop(face: Face, rect) -> Face:
    """The face as it sits in the edited crop (EDIT_SIDE pixels)."""
    left, top, side = rect
    scale = EDIT_SIDE / side
    x, y, width, height = face.box
    return Face(((x - left) * scale, (y - top) * scale, width * scale, height * scale),
                tuple(((px - left) * scale, (py - top) * scale) for px, py in face.points))


def test_the_crop_is_a_square_around_the_face_kept_inside_the_picture():
    assert crop_rect((400, 500), FACE) == (50, 60, 300)
    # A face at the picture's edge: the square is pushed inside and the face is no longer in the middle.
    edge = Face((0.0, 10.0, 100.0, 120.0), FACE.points)
    rect = crop_rect((400, 500), edge)
    assert rect == (0, 0, 300) and position(rect, edge) == '左侧' and position(crop_rect((400, 500), FACE), FACE) == '中央'
    assert crop_rect((200, 200), FACE)[2] == 200                 # never larger than the picture
    edited = Image.open(io.BytesIO(crop(picture('red'), crop_rect((400, 500), FACE))))
    assert edited.size == (EDIT_SIDE, EDIT_SIDE)


def test_points_are_ordered_left_to_right_whatever_the_detector_reports():
    face = face_from_analysis({'box': [150, 150, 250, 270], 'points': [[225, 195], [175, 195], [200, 225], [220, 250], [180, 250]]})
    assert face == FACE


def test_only_the_face_of_the_edit_is_laid_back():
    rect = crop_rect((400, 500), FACE)
    fixer = FaceFixer(all_skin, detect=lambda image: [in_crop(FACE, rect)])
    result = fixer.lay_back(picture('red'), png(picture('blue', (EDIT_SIDE, EDIT_SIDE))), rect, FACE)
    red, _green, blue = result.getpixel((200, 215))
    assert blue > 240 and red < 15                              # the edited face
    assert result.getpixel((200, 115)) == (255, 0, 0)          # above the face: hair stays the picture's
    assert result.getpixel((10, 10)) == (255, 0, 0)            # outside the crop: untouched


def test_hair_inside_the_face_ellipse_stays_the_pictures():
    rect = crop_rect((400, 500), FACE)

    def fringe(image, box):
        skin = np.ones((image.height, image.width), np.float32)
        # The picture's fringe down to y=190; the edit is parsed in its own (crop) pixels.
        skin[:190 if image.width == 400 else round((190 - rect[1]) * EDIT_SIDE / rect[2])] = 0
        return skin

    fixer = FaceFixer(fringe, detect=lambda image: [in_crop(FACE, rect)])
    result = fixer.lay_back(picture('red'), png(picture('blue', (EDIT_SIDE, EDIT_SIDE))), rect, FACE)
    assert result.getpixel((200, 170)) == (255, 0, 0) and result.getpixel((200, 235))[2] > 240


def test_an_edit_that_moved_the_head_is_not_laid_back():
    rect = crop_rect((400, 500), FACE)
    drawn = in_crop(FACE, rect)
    # The mouth dropped: another expression, the points no longer fit a move, turn and scale.
    other = Face(drawn.box, drawn.points[:3] + ((drawn.points[3][0], drawn.points[3][1] + 120), (drawn.points[4][0], drawn.points[4][1] + 120)))
    for found in ([], [other]):
        with pytest.raises(FaceFixError) as raised:
            FaceFixer(all_skin, detect=lambda image, found=found: found).lay_back(
                picture('red'), png(picture('blue', (EDIT_SIDE, EDIT_SIDE))), rect, FACE)
        assert raised.value.code == 'face_lost'


def test_the_prompt_assigns_each_attribute_to_its_image():
    prompt = compose_face_fix_prompt('groom', '右侧')
    assert prompt.startswith('第一张图是需要修正的婚纱照局部裁剪，目标是画面右侧的新郎。第二张图是这位新人本人的原始照片')
    assert '长相依据第二张图' in prompt and '妆容和精修依据第一张图' in prompt and '不新增美白或磨皮' in prompt
    assert '换脸' not in prompt
    with pytest.raises(ValueError):
        compose_face_fix_prompt('bride', '上方')


# Both faces found in the 1 x 1 test pictures.
FOUND = {'bride': {'box': [0, 0, 1, 1], 'points': [[0, 0]] * 5}, 'groom': {'box': [0, 0, 1, 1], 'points': [[0, 0]] * 5}}


class FindingSwapper(ScriptedFaceSwapper):
    def __init__(self, found=FOUND):
        super().__init__()
        self.found = found

    def faces(self, target):
        return self.found


class MarkingFixer:
    """Lays nothing back but records the call; refuses the roles in fail (by the order of calls)."""

    def __init__(self, fail=()):
        self.fail = list(fail)
        self.calls = 0

    def lay_back(self, picture, edited, rect, face):
        self.calls += 1
        if self.calls in self.fail:
            raise FaceFixError('face_lost', '修图结果里找不到对得上的脸')
        return picture


def fix_task(client, batch_id):
    return next(task for task in client.get('/api/tasks', params={'batch_id': batch_id}).json() if task['regenerate_mode'] == 'face_fix')


def test_every_picture_gets_an_ai_face_fix_from_the_uploaded_photos(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, face_swapper=FindingSwapper(), face_fixer=MarkingFixer(), auto_face_fix='ai',
                     concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 2
        shot, *edits = generator.calls
        assert len(edits) == 2 and generator.aspect_ratios[1:] == ['1:1', '1:1']
        # Each edit: the crop, then that person's uploaded photo (never the look).
        uploads = {'bride': project['person_a']['asset_id'], 'groom': project['person_b']['asset_id']}
        for (images, prompt, _at), role in zip(edits, ('bride', 'groom')):
            assert len(images) == 2 and uploaded_ids(app, [images[1].stem]) == [uploads[role]] and images[1].stem == uploads[role]
            assert prompt == compose_face_fix_prompt(role, '中央')
        task = fix_task(client, batch['id'])
        with app.state.db.connect() as conn:
            row = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
        assert json.loads(row['reference_image_ids']) == [uploads['bride'], uploads['groom']]
        assert json.loads(row['model_config_snapshot'])['faces'] == {'bride': 'fixed', 'groom': 'fixed'}
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert slot['current_version']['version_number'] == 2 and slot['current_version']['source_version_id']
        # The fixed picture keeps its sample's looks.
        assert slot['look_ids'] is not None


def test_a_face_that_cannot_be_laid_back_stays_and_two_fail_the_fix(app_factory):
    with app_factory(generator=ScriptedGenerator(), face_swapper=FindingSwapper(), face_fixer=MarkingFixer(fail=[2]),
                     auto_face_fix='ai', concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        with app.state.db.connect() as conn:
            row = conn.execute("SELECT model_config_snapshot FROM generation_tasks WHERE regenerate_mode='face_fix'").fetchone()
        assert json.loads(row[0])['faces'] == {'bride': 'fixed', 'groom': 'face_lost'}
    with app_factory(generator=ScriptedGenerator(), face_swapper=FindingSwapper(), face_fixer=MarkingFixer(fail=[1, 2]),
                     auto_face_fix='ai', concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], request_id='both-fail', style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        assert fix_task(client, batch['id'])['error_code'] == 'face_fix_failed'
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert slot['current_version']['version_number'] == 1      # the original is kept


def test_no_faces_found_fails_without_an_edit(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, face_swapper=FindingSwapper({'bride': None, 'groom': None}),
                     face_fixer=MarkingFixer(), auto_face_fix='ai', concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        assert fix_task(client, batch['id'])['error_code'] == 'face_not_found' and len(generator.calls) == 1


def test_the_setting_picks_the_ai_fix_the_swap_or_nothing(app_factory):
    swapper = FindingSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, face_fixer=MarkingFixer(), auto_face_fix='off',
                     concurrency=1) as (client, _app):
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
    generator = ScriptedGenerator()
    with app_factory(generator=generator, face_swapper=FindingSwapper(), face_fixer=MarkingFixer(), concurrency=1) as (client, app):
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
                row = conn.execute("SELECT source_version_id FROM generation_tasks WHERE batch_id=?", (response.json()['id'],)).fetchone()
            assert row['source_version_id'] == original
        batch = client.post('/api/slots/batch-action', json={'client_request_id': 'many', 'slot_ids': [slot['id']], 'action': 'face_fix'})
        assert batch.status_code == 201, batch.text


def test_without_facefusion_there_is_no_ai_fix(app_factory):
    with app_factory(generator=ScriptedGenerator(), face_fixer=MarkingFixer(), auto_face_fix='ai', concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        assert client.get(f"/api/batches/{batch['id']}").json()['total'] == 1
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        response = client.post(f"/api/slots/{slot['id']}/face-fix", json={'client_request_id': 'none'})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'face_swap_unavailable'


def test_the_prompt_matches_the_docs():
    from pathlib import Path
    import re
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
    block = re.search(r'### 9\.2 编辑提示词.*?```text\n(.*?)\n```', doc, re.S).group(1)
    assert block == compose_face_fix_prompt('bride', '中央')
