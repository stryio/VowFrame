"""Upload checks and head-and-shoulders cropping (app/photo_prep.py)."""

import io

import pytest
from PIL import Image

from app.photo_prep import (FACE_TOO_SMALL, NO_FACE, OUTPUT_LONG_SIDE, SEVERAL_PEOPLE, Face, FacePhotoPreparer,
                            PhotoRejected, _head_and_shoulders)
from conftest import create_project


def photo(width=3000, height=4000, mode='RGB') -> bytes:
    output = io.BytesIO()
    Image.new(mode, (width, height), 'white').save(output, 'PNG' if 'A' in mode else 'JPEG')
    return output.getvalue()


def preparer(*faces_in_photo_pixels, long_side=4000):
    """Faces are given in photo pixels; the detector sees a copy reduced to 1280 px on the long side."""
    scale = min(1.0, 1280 / long_side)
    seen = []

    def detect(image):
        seen.append(image.size)
        return [Face(f.x * scale, f.y * scale, f.width * scale, f.height * scale) for f in faces_in_photo_pixels]

    return FacePhotoPreparer(detect), seen


def test_a_small_face_in_a_large_photo_is_cropped_to_head_and_shoulders():
    prep, seen = preparer(Face(1300, 800, 400, 500))
    result = Image.open(io.BytesIO(prep.prepare(photo())))
    assert seen == [(960, 1280)]  # detection runs on a reduced copy
    # 3 face widths wide; 0.7 face heights above the face and 1.3 below it.
    assert result.size == (1200, 1500)
    assert result.format == 'JPEG'


def test_crop_is_clipped_to_the_photo_and_never_padded():
    assert _head_and_shoulders(Face(50, 40, 400, 500), (1000, 1000)) == (0, 0, 850, 1000)


def test_large_crops_are_reduced_and_transparent_photos_stay_png():
    prep, _ = preparer(Face(1000, 1000, 2000, 2400), long_side=6000)
    result = Image.open(io.BytesIO(prep.prepare(photo(4000, 6000, 'RGBA'))))
    assert max(result.size) == OUTPUT_LONG_SIDE and result.format == 'PNG'


@pytest.mark.parametrize('faces, message', [
    ((), NO_FACE),
    ((Face(1300, 800, 400, 500), Face(400, 900, 300, 300)), SEVERAL_PEOPLE),
    ((Face(1300, 800, 100, 130),), FACE_TOO_SMALL),
])
def test_unusable_photos_are_rejected_with_a_reason(faces, message):
    prep, _ = preparer(*faces)
    with pytest.raises(PhotoRejected) as raised:
        prep.prepare(photo())
    assert raised.value.message == message


def test_small_faces_in_the_background_do_not_count_as_another_person():
    prep, _ = preparer(Face(1300, 800, 400, 500), Face(200, 300, 150, 150))
    assert Image.open(io.BytesIO(prep.prepare(photo()))).size == (1200, 1500)


def test_the_bundled_face_detector_loads_and_finds_no_face_in_a_blank_photo():
    with pytest.raises(PhotoRejected) as raised:
        FacePhotoPreparer().prepare(photo(800, 1000))
    assert raised.value.message == NO_FACE


def test_upload_stores_the_untouched_photo_and_uses_the_prepared_one(app_factory):
    with app_factory() as (client, app):
        project = create_project(client, upload=False)
        prepared = photo(600, 800)
        app.state.photo_preparer = type('Fixed', (), {'prepare': lambda self, content: prepared})()
        response = client.post(f"/api/projects/{project['id']}/images", data={'person': 'a'},
                               files={'file': ('a.jpg', photo(), 'image/jpeg')})
        assert response.status_code == 200, response.text
        asset_id = response.json()['person_a']['asset_id']
        with app.state.db.connect() as conn:
            asset = conn.execute('SELECT * FROM image_assets WHERE id=?', (asset_id,)).fetchone()
            upload = conn.execute('SELECT * FROM image_assets WHERE id=?', (asset['source_asset_id'],)).fetchone()
        assert asset['asset_type'] == 'original' and upload['asset_type'] == 'upload'
        assert client.get(f'/api/assets/{asset_id}').content == prepared


def test_upload_of_an_unusable_photo_is_refused_and_stores_nothing(app_factory):
    with app_factory() as (client, app):
        project = create_project(client, upload=False)
        app.state.photo_preparer = FacePhotoPreparer(lambda image: [])
        response = client.post(f"/api/projects/{project['id']}/images", data={'person': 'a'},
                               files={'file': ('a.jpg', photo(), 'image/jpeg')})
        assert response.status_code == 422
        assert response.json()['detail'] == {'code': 'unusable_photo', 'message': NO_FACE}
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0] == 0
