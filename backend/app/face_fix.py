"""AI face fix (V7.1): make each person's face in a finished picture look like their uploaded photo, keeping the
makeup and retouching the picture already has (docs/prompts.md section 9).

FaceFusion's swap re-renders the face from the uploaded photo, and the bare-faced photo washes the makeup out. Here
the image model makes the likeness edit, on a crop around one face, and only that face is laid back:

1. crop(): a square around the face, CROP_SIDE face-box sides wide, scaled to EDIT_SIDE, so a small face in a wide
   shot is edited at a usable size. Which face is the bride's and which the groom's comes from FaceFusion's
   face analysis (app.face_swap), never from the image model.
2. The crop and that person's uploaded photo go to the edit endpoint with the face-fix prompt. No mask: 88API
   takes none, and a mask would only be guidance anyway (V6.0).
3. lay_back(): the edited crop's face is found again and aligned on its five points with a move, turn and scale
   only, so the corrected features keep their shape. Only pixels that are face in both the picture and the edit
   (SegFace, app.face_parse), inside an ellipse around the face, are laid back with a soft edge; hair, headwear,
   hands, the other person and the background stay the picture's own pixels.
"""

from __future__ import annotations

import io
import json
import threading
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageOps

from .face_parse import FaceParseError
from .photo_prep import MIN_SCORE, MODEL_PATH

CROP_SIDE = 2.5   # side of the square crop, in the face box's longest side (Codex's starting point, untuned)
EDIT_SIDE = 1024  # the crop is edited at this size (1:1)
# The most that is laid back, around the face box, in box widths/heights: forehead to chin, cheek to cheek. Inside
# it only face is laid back, so hair and occluders inside the ellipse stay the picture's.
KEEP_CENTRE_Y, KEEP_HALF_WIDTH, KEEP_HALF_HEIGHT = 0.45, 0.58, 0.64
SKIN = 0.5               # face probability above which a pixel counts as face
SHRINK, FEATHER = 0.03, 0.03  # the laid-back face stops this far inside the face edge and fades over FEATHER (face widths)
MAX_MISFIT = 0.08        # mean distance of the five points after alignment, in face widths: the edit redrew the head
CENTRE_OFFSET = 0.12     # a face this far from the crop's centre (in crop sides) is described as left or right


@dataclass(frozen=True)
class Face:
    """A face: its box (x, y, width, height) and five points (both eyes left to right, nose tip, both mouth
    corners left to right)."""
    box: tuple[float, float, float, float]
    points: tuple[tuple[float, float], ...]


class FaceFixError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


FACE_LOST = ('face_lost', '修图结果里找不到对得上的脸（表情或角度变了），这张脸保留原样')


def _ordered(points) -> tuple[tuple[float, float], ...]:
    """Eyes and mouth corners left to right as seen in the picture: detectors disagree on which comes first."""
    points = [(float(x), float(y)) for x, y in points]
    eyes = sorted(points[:2])
    mouth = sorted(points[3:5])
    return (eyes[0], eyes[1], points[2], mouth[0], mouth[1])


def face_from_analysis(value: dict) -> Face:
    """A face as FaceFusion's analysis reports it: box (x1, y1, x2, y2) and five points."""
    x1, y1, x2, y2 = (float(item) for item in value['box'])
    return Face((x1, y1, x2 - x1, y2 - y1), _ordered(value['points']))


def crop_rect(size: tuple[int, int], face: Face) -> tuple[int, int, int]:
    """(left, top, side) of the square crop around the face, kept inside the picture."""
    x, y, width, height = face.box
    side = min(round(CROP_SIDE * max(width, height)), size[0], size[1])
    left = min(max(0, round(x + width / 2 - side / 2)), size[0] - side)
    top = min(max(0, round(y + height / 2 - side / 2)), size[1] - side)
    return left, top, side


def position(rect: tuple[int, int, int], face: Face) -> str:
    """Where the face sits in its crop, for the prompt: a face near the picture's edge is not in the middle."""
    left, _top, side = rect
    offset = (face.box[0] + face.box[2] / 2 - left) / side - 0.5
    if offset < -CENTRE_OFFSET:
        return '左侧'
    if offset > CENTRE_OFFSET:
        return '右侧'
    return '中央'


def crop(picture: Image.Image, rect: tuple[int, int, int]) -> bytes:
    left, top, side = rect
    output = io.BytesIO()
    picture.crop((left, top, left + side, top + side)).resize((EDIT_SIDE, EDIT_SIDE), Image.Resampling.LANCZOS).save(output, 'PNG')
    return output.getvalue()


def open_rgb(content: bytes) -> Image.Image:
    with Image.open(io.BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source)
        image.load()
    return image.convert('RGB')


def _yunet_faces() -> Callable[[Image.Image], list[Face]]:
    import cv2
    import numpy as np

    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), '', (320, 320), MIN_SCORE, 0.3, 50)
    lock = threading.Lock()

    def detect(image: Image.Image) -> list[Face]:
        array = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        with lock:
            detector.setInputSize((array.shape[1], array.shape[0]))
            _, rows = detector.detect(array)
        return [Face(tuple(map(float, row[:4])), _ordered([(row[i], row[i + 1]) for i in range(4, 14, 2)]))
                for row in ([] if rows is None else rows)]

    return detect


def _ellipse(size: tuple[int, int], face: Face):
    import cv2
    import numpy as np

    region = np.zeros((size[1], size[0]), np.float32)
    x, y, width, height = face.box
    # Halfway between the box centre and the features: on a turned face the box lags behind the nose side.
    features_x = sum(px for px, _py in face.points) / len(face.points)
    centre = (round((x + width / 2 + features_x) / 2), round(y + height * KEEP_CENTRE_Y))
    cv2.ellipse(region, centre, (round(width * KEEP_HALF_WIDTH), round(height * KEEP_HALF_HEIGHT)), 0, 0, 360, 1.0, -1)
    return region


class FaceFixer:
    """skin: the face probability of every pixel of an image around a face box (FaceParser.face_skin)."""

    def __init__(self, skin: Callable[[Image.Image, tuple], object], detect: Callable[[Image.Image], list[Face]] | None = None):
        self._skin = skin
        self._detect = detect
        self._lock = threading.Lock()

    def _faces(self, image: Image.Image) -> list[Face]:
        with self._lock:
            if self._detect is None:
                self._detect = _yunet_faces()
        return self._detect(image)

    def _face_skin(self, image: Image.Image, face: Face):
        try:
            return self._skin(image, face.box) > SKIN
        except FaceParseError as error:
            raise FaceFixError(error.code, error.message) from None

    def lay_back(self, picture: Image.Image, edited: bytes, rect: tuple[int, int, int], face: Face) -> Image.Image:
        """The picture with the edited crop's face laid over this face; FaceFixError when they do not line up."""
        import cv2
        import numpy as np

        left, top, side = rect
        drawn_image = open_rgb(edited).resize((EDIT_SIDE, EDIT_SIDE), Image.Resampling.LANCZOS)
        scale = side / EDIT_SIDE
        # The face the edit drew where this face was: the detected face whose nose is nearest.
        nose = ((face.points[2][0] - left) / scale, (face.points[2][1] - top) / scale)
        candidates = [(np.hypot(item.points[2][0] - nose[0], item.points[2][1] - nose[1]), index, item)
                      for index, item in enumerate(self._faces(drawn_image))]
        if not candidates:
            raise FaceFixError(*FACE_LOST)
        distance, _index, drawn = min(candidates)
        if distance > 0.5 * face.box[2] / scale:
            raise FaceFixError(*FACE_LOST)
        source_points = np.array(drawn.points, np.float32)
        target_points = np.array(face.points, np.float32)
        # Move, turn and scale only: the corrected features keep their shape and proportions.
        matrix, _ = cv2.estimateAffinePartial2D(source_points, target_points, method=cv2.LMEDS)
        if matrix is None:
            raise FaceFixError(*FACE_LOST)
        moved = source_points @ matrix[:, :2].T + matrix[:, 2]
        if float(np.linalg.norm(moved - target_points, axis=1).mean()) / face.box[2] > MAX_MISFIT:
            raise FaceFixError(*FACE_LOST)
        size = picture.size
        keep = _ellipse(size, face) * self._face_skin(picture, face).astype(np.float32)
        drawn_face = cv2.warpAffine(self._face_skin(drawn_image, drawn).astype(np.float32), matrix, size,
                                    flags=cv2.INTER_NEAREST, borderValue=0)
        both = np.minimum(keep, drawn_face)
        radius = max(1, round(face.box[2] * SHRINK))
        both = cv2.erode(both, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1)))
        weight = cv2.GaussianBlur(both, (0, 0), max(1.0, face.box[2] * FEATHER))[..., None]
        laid = cv2.warpAffine(np.asarray(drawn_image, np.float32), matrix, size, flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REFLECT)
        result = laid * weight + np.asarray(picture, np.float32) * (1 - weight)
        return Image.fromarray(np.clip(result + 0.5, 0, 255).astype(np.uint8))


def face_fix_snapshot(model: str) -> str:
    """Model record stored on face-fix tasks; the executor adds 'faces': per role 'fixed', 'not_found' or why the
    edited face was not laid back."""
    return json.dumps({'generator': 'face_fix', 'model': model, 'crop_side': CROP_SIDE, 'edit_side': EDIT_SIDE},
                      ensure_ascii=False)
