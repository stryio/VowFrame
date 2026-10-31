"""Check an uploaded person photo and crop it to head and shoulders before it becomes a reference.

The image model shrinks every reference to about 1024 px before reading it. In a phone photo of a whole person the
face is a few percent of the frame and ends up some 100 px wide, too small for the model to keep the likeness.
Cropping to head and shoulders spends those pixels on the face and drops other people, clothes and background.
No AI upscaling or face restoration: those invent detail and would change the face being copied.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from PIL import Image, ImageOps

MODEL_PATH = Path(__file__).parent / 'assets' / 'face_detection_yunet_2023mar.onnx'
DETECT_LONG_SIDE = 1280  # faces are found on a reduced copy; boxes are scaled back to the photo
MIN_SCORE = 0.8
MIN_FACE_SIDE = 120  # shorter side of the face box, in photo pixels
OTHER_FACE_SHARE = 0.25  # another face this large (by area, relative to the main one) means another person
OUTPUT_LONG_SIDE = 2048
# Head and shoulders around the face box, in face widths/heights: hair and hairline above, shoulders below.
CROP_WIDTH, CROP_ABOVE, CROP_BELOW = 3.0, 0.7, 1.3

NO_FACE = '没有检测到人脸，请换一张五官清晰、正脸或接近正脸的照片'
SEVERAL_PEOPLE = '照片里不止一个人，请换一张只有本人的照片，或先把其他人裁掉'
FACE_TOO_SMALL = '脸在照片里太小或照片太模糊，请换一张离得更近、更清晰的照片'


@dataclass(frozen=True)
class Face:
    x: float
    y: float
    width: float
    height: float


class PhotoRejected(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class PhotoPreparer(Protocol):
    def prepare(self, content: bytes) -> bytes: ...


class PassthroughPhotoPreparer:
    """Keeps the upload as it is (tests and tools that have no real faces)."""

    def prepare(self, content: bytes) -> bytes:
        return content


def _yunet_detector() -> Callable[[Image.Image], list[Face]]:
    import cv2
    import numpy as np

    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), '', (320, 320), MIN_SCORE, 0.3, 50)
    lock = threading.Lock()  # one detector object, uploads arrive on worker threads

    def detect(image: Image.Image) -> list[Face]:
        array = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        with lock:
            detector.setInputSize((array.shape[1], array.shape[0]))
            _, rows = detector.detect(array)
        return [Face(*map(float, row[:4])) for row in ([] if rows is None else rows)]

    return detect


class FacePhotoPreparer:
    def __init__(self, detect: Callable[[Image.Image], list[Face]] | None = None):
        self._detect = detect or _yunet_detector()

    def prepare(self, content: bytes) -> bytes:
        with Image.open(io.BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source)
            icc_profile = source.info.get('icc_profile')
            image.load()
        has_alpha = 'A' in image.getbands()
        image = image.convert('RGBA' if has_alpha else 'RGB')
        face = self._main_face(image)
        image = image.crop(_head_and_shoulders(face, image.size))
        if max(image.size) > OUTPUT_LONG_SIDE:
            image.thumbnail((OUTPUT_LONG_SIDE, OUTPUT_LONG_SIDE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        if has_alpha:
            image.save(output, 'PNG', icc_profile=icc_profile)
        else:
            image.save(output, 'JPEG', quality=95, subsampling=0, icc_profile=icc_profile)
        return output.getvalue()

    def _main_face(self, image: Image.Image) -> Face:
        scale = min(1.0, DETECT_LONG_SIDE / max(image.size))
        reduced = image.convert('RGB')
        if scale < 1.0:
            reduced = reduced.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.BILINEAR)
        faces = sorted((Face(f.x / scale, f.y / scale, f.width / scale, f.height / scale) for f in self._detect(reduced)),
                       key=lambda f: f.width * f.height, reverse=True)
        if not faces:
            raise PhotoRejected(NO_FACE)
        main = faces[0]
        if any(other.width * other.height >= OTHER_FACE_SHARE * main.width * main.height for other in faces[1:]):
            raise PhotoRejected(SEVERAL_PEOPLE)
        if min(main.width, main.height) < MIN_FACE_SIDE:
            raise PhotoRejected(FACE_TOO_SMALL)
        return main


def _head_and_shoulders(face: Face, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Crop box (left, top, right, bottom), clipped to the photo; the photo is never padded."""
    width, height = size
    centre = face.x + face.width / 2
    left = max(0, round(centre - face.width * CROP_WIDTH / 2))
    right = min(width, round(centre + face.width * CROP_WIDTH / 2))
    top = max(0, round(face.y - face.height * CROP_ABOVE))
    bottom = min(height, round(face.y + face.height * (1 + CROP_BELOW)))
    return left, top, right, bottom
