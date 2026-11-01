"""The bundled SegFace face-parsing model (V6.3), kept for skin retouching (app/face_parse.py)."""

import numpy as np
from PIL import Image

from app.face_parse import FaceParser

# A face box (x, y, width, height) in a 400 x 500 picture.
BOX = (150.0, 150.0, 100.0, 120.0)


def test_the_bundled_face_parser_runs_and_gives_a_probability_per_pixel():
    image = Image.new('RGB', (400, 500), (230, 200, 180))
    skin = FaceParser().face_skin(image, BOX)
    assert skin.shape == (500, 400) and skin.dtype == np.float32
    assert 0 <= skin.min() and skin.max() <= 1.0001
    assert skin[:5, :5].max() == 0                                    # outside the square parsed around the face
