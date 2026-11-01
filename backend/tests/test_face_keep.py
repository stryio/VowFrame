"""Keeping the uploaded face in a look (V6.0): the edit mask, and the photo's face laid back over the edit; only
face skin is kept, never a fringe (V6.2)."""

import io

import numpy as np
import pytest
from PIL import Image

from app.face_parse import FaceParseError

from app.face_keep import FaceKeepError, FaceShape, YuNetFaceKeeper
from conftest import ScriptedGenerator, create_project, wait_until

# A face box and its five points (eyes, nose tip, mouth corners) in a 400 x 500 picture.
FACE = FaceShape((150.0, 150.0, 100.0, 120.0), ((175.0, 195.0), (225.0, 195.0), (200.0, 225.0), (180.0, 250.0), (220.0, 250.0)))


def picture(color, size=(400, 500)) -> bytes:
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, 'PNG')
    return output.getvalue()


def shifted(face: FaceShape, dx: float, dy: float, scale: float = 1.0) -> FaceShape:
    x, y, width, height = face.box
    return FaceShape((x * scale + dx, y * scale + dy, width * scale, height * scale),
                     tuple((px * scale + dx, py * scale + dy) for px, py in face.points))


def all_skin(image, box):
    return np.ones((image.height, image.width), np.float32)


def fringe_above(y):
    """Face skin everywhere except hair above y (a fringe over the forehead)."""
    def skin(image, box):
        result = np.ones((image.height, image.width), np.float32)
        result[:round(y)] = 0
        return result
    return skin


def keeper(*faces_per_call, skin=all_skin):
    """A keeper whose detector returns the given faces, one list per call, in picture pixels."""
    calls = list(faces_per_call)
    return YuNetFaceKeeper(skin, detect=lambda image: calls.pop(0))


def test_the_mask_keeps_the_face_and_frees_the_rest():
    mask, face = keeper([FACE]).mask(picture('red'))
    image = Image.open(io.BytesIO(mask))
    assert image.mode == 'RGBA' and image.size == (400, 500) and face == FACE
    alpha = image.getchannel('A')
    assert alpha.getpixel((200, 215)) == 255                      # between the eyes and the mouth: kept
    assert alpha.getpixel((200, 160)) == 255                      # the forehead, without a fringe
    assert alpha.getpixel((200, 120)) == 0                        # above the face box: hair may change
    assert alpha.getpixel((140, 215)) == 0                        # beside the cheek: hair may fall there
    assert alpha.getpixel((20, 480)) == 0                         # shoulders and background


def test_a_fringe_in_the_photo_is_left_free_in_the_mask():
    mask, _face = keeper([FACE], skin=fringe_above(190)).mask(picture('red'))
    alpha = Image.open(io.BytesIO(mask)).getchannel('A')
    assert alpha.getpixel((200, 170)) == 0 and alpha.getpixel((200, 230)) == 255


def test_a_missing_segmentation_model_stops_the_look_before_the_edit():
    def unavailable(image, box):
        raise FaceParseError('face_model_download_failed', '人脸分割模型下载失败')
    with pytest.raises(FaceKeepError) as raised:
        keeper([FACE], skin=unavailable).mask(picture('red'))
    assert raised.value.code == 'face_model_download_failed'


def test_a_photo_without_a_face_gets_no_mask():
    with pytest.raises(FaceKeepError) as raised:
        keeper([]).mask(picture('red'))
    assert raised.value.code == 'look_no_face'


def test_the_photo_face_is_laid_back_where_the_edit_drew_it():
    # The edit came back larger and moved; the photo is red, the edit blue.
    drawn = shifted(FACE, 30, -10, 1.5)
    result = Image.open(io.BytesIO(keeper([drawn]).restore(picture('red'), picture('blue', (600, 750)), FACE)))
    assert result.size == (600, 750)
    centre = (round(200 * 1.5 + 30), round(215 * 1.5 - 10))
    red, _green, blue = result.getpixel(centre)
    assert red > 240 and blue < 15                                 # the photo's face
    red, _green, blue = result.getpixel((30, 30))
    assert red < 15 and blue > 240                                 # the edit's background and hair


def test_neither_the_photos_fringe_nor_the_edits_new_fringe_is_covered():
    # The photo has a fringe down to y=190; laying it back over a bare forehead would leave a dark patch.
    result = Image.open(io.BytesIO(keeper([FACE], skin=fringe_above(190)).restore(picture('red'), picture('blue'), FACE)))
    assert result.getpixel((200, 170))[2] > 240 and result.getpixel((200, 235))[0] > 240
    # The edit drew a fringe down to y=190 over the photo's bare forehead; it stays.
    calls = iter([all_skin, fringe_above(190)])
    result = Image.open(io.BytesIO(keeper([FACE], skin=lambda image, box: next(calls)(image, box)).restore(
        picture('red'), picture('blue'), FACE)))
    assert result.getpixel((200, 170))[2] > 240 and result.getpixel((200, 235))[0] > 240


def test_an_edit_whose_face_cannot_be_matched_fails_instead_of_pasting():
    with pytest.raises(FaceKeepError) as raised:
        keeper([]).restore(picture('red'), picture('blue'), FACE)
    assert raised.value.code == 'look_face_lost'
    # Another expression: the mouth moved down a lot, so the points no longer fit.
    other = FaceShape(FACE.box, FACE.points[:3] + ((180.0, 290.0), (220.0, 290.0)))
    with pytest.raises(FaceKeepError):
        keeper([other]).restore(picture('red'), picture('blue'), FACE)


class MarkingKeeper:
    """Masks with fixed bytes and marks every restored picture, so the test sees both steps were used."""

    def __init__(self, fail=False):
        self.fail = fail
        self.restored = []

    def mask(self, photo):
        return b'MASK', FACE

    def restore(self, photo, edited, face):
        if self.fail:
            raise FaceKeepError('look_face_lost', '修图结果里找不到对得上的脸')
        self.restored.append(face)
        return picture('green')


def test_a_look_is_edited_with_the_mask_and_keeps_the_face(app_factory):
    face_keeper = MarkingKeeper()
    with app_factory(generator=ScriptedGenerator(), face_keeper=face_keeper) as (client, app):
        project = create_project(client)
        client.post(f"/api/projects/{project['id']}/looks",
                    json={'client_request_id': 'masked', 'person': 'a', 'items': [{'hairstyle_id': 'low_bun'}]})
        look = wait_until(lambda: client.get(f"/api/projects/{project['id']}").json()['looks'][0],
                          lambda look: look['status'] not in ('waiting', 'running'))
        assert look['status'] == 'ready'
        assert app.state.stylist.masks == [b'MASK'] and face_keeper.restored == [FACE]
        image = client.get(f"/api/assets/{look['asset_id']}")
        assert Image.open(io.BytesIO(image.content)).getpixel((0, 0)) == (0, 128, 0)


def test_a_look_whose_face_cannot_be_kept_fails_with_the_reason(app_factory):
    with app_factory(generator=ScriptedGenerator(), face_keeper=MarkingKeeper(fail=True)) as (client, _app):
        project = create_project(client)
        client.post(f"/api/projects/{project['id']}/looks",
                    json={'client_request_id': 'lost', 'person': 'a', 'items': [{'hairstyle_id': 'low_bun'}]})
        look = wait_until(lambda: client.get(f"/api/projects/{project['id']}").json()['looks'][0],
                          lambda look: look['status'] not in ('waiting', 'running'))
        assert look['status'] == 'failed' and look['error_code'] == 'look_face_lost'
