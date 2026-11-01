"""Keep the uploaded face in a look (V6.0): only the hair is redrawn.

The image edit endpoint redraws the whole picture even when asked to change only the hair, and every redraw moves
the face a little away from the person; asking for a bold new hairstyle made it move a lot (V5.2). So a look is
made in two parts around the edit:

1. mask(): the face inside the hairline, from the eyebrows down to the chin, is marked as the part to keep. The
   edit endpoint takes the mask only as guidance, so this alone does not keep the face.
2. restore(): the face of the uploaded photo is laid back over the edited picture, aligned on the eyes, nose and
   mouth the edit drew, with a soft edge. Inside that region the look is the uploaded photo's pixels.

V6.2: only face skin is kept. An ellipse alone laid a fringe of the photo back over the new hairstyle as a dark
patch on the forehead, and cut off a fringe the new hairstyle has. A segmentation model (app/face_parse.py) marks
face skin in the photo and in the edit; a pixel is laid back only where it is face skin in both, inside a bounding
ellipse. Makeup and retouching are not part of a look; the shot draws them (docs/prompts.md section 8).
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from PIL import Image, ImageOps

from .face_parse import FaceParseError
from .photo_prep import MIN_SCORE, MODEL_PATH

DETECT_LONG_SIDE = 1280  # faces are found on a reduced copy, as in photo_prep
# The most that can be kept, around the face box, in box widths/heights: an ellipse from the top of the forehead
# to the chin and out to the cheeks. Inside it only face skin is kept (V6.2), so it no longer has to stop short of
# a possible fringe.
KEEP_CENTRE_Y, KEEP_HALF_WIDTH, KEEP_HALF_HEIGHT = 0.45, 0.55, 0.62
SKIN = 0.5  # face-skin probability above which a pixel counts as face
# The laid-back face stops this far inside the face-skin edge, and fades out over FEATHER, so no hair next to the
# edge is carried along (in face widths).
SHRINK, FEATHER = 0.05, 0.025
MAX_MISFIT = 0.08  # mean distance of the five points after alignment, in face widths: the face was redrawn otherwise


@dataclass(frozen=True)
class FaceShape:
    """A detected face: its box (x, y, width, height) and five points (eyes, nose tip, mouth corners)."""
    box: tuple[float, float, float, float]
    points: tuple[tuple[float, float], ...]


class FaceKeepError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


NO_FACE = ('look_no_face', '照片里没有检测到人脸，无法只修改头发；请换一张五官清晰的正面照后重试')
FACE_LOST = ('look_face_lost', '修图结果里找不到对得上的脸，没法贴回原来的脸；这次修图可能已经扣费，请重试')


class FaceKeeper(Protocol):
    def mask(self, photo: bytes) -> tuple[bytes | None, FaceShape | None]: ...

    def restore(self, photo: bytes, edited: bytes, face: FaceShape | None) -> bytes: ...


class PassthroughFaceKeeper:
    """Sends no mask and keeps the edit as it is (tests whose photos are placeholders without faces)."""

    def mask(self, photo: bytes) -> tuple[bytes | None, FaceShape | None]:
        return None, None

    def restore(self, photo: bytes, edited: bytes, face: FaceShape | None) -> bytes:
        return edited


def _yunet_faces() -> Callable[[Image.Image], list[FaceShape]]:
    import cv2
    import numpy as np

    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), '', (320, 320), MIN_SCORE, 0.3, 50)
    lock = threading.Lock()

    def detect(image: Image.Image) -> list[FaceShape]:
        array = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        with lock:
            detector.setInputSize((array.shape[1], array.shape[0]))
            _, rows = detector.detect(array)
        return [FaceShape(tuple(map(float, row[:4])), tuple((float(row[i]), float(row[i + 1])) for i in range(4, 14, 2)))
                for row in ([] if rows is None else rows)]

    return detect


def _open(content: bytes) -> Image.Image:
    # Same orientation as the picture that is sent (storage.outgoing_image), so the mask lines up with it.
    with Image.open(io.BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source)
        image.load()
    return image.convert('RGB')


def _keep_ellipse(face: FaceShape) -> tuple[tuple[float, float], tuple[float, float]]:
    x, y, width, height = face.box
    # Halfway between the box centre and the features: on a turned face the box lags behind the nose side, and
    # the features alone would leave the far cheek out.
    features_x = sum(px for px, _py in face.points) / len(face.points)
    centre_x = (x + width / 2 + features_x) / 2
    return (centre_x, y + height * KEEP_CENTRE_Y), (width * KEEP_HALF_WIDTH, height * KEEP_HALF_HEIGHT)


def _ellipse(size: tuple[int, int], face: FaceShape):
    import cv2
    import numpy as np

    region = np.zeros((size[1], size[0]), np.float32)
    (cx, cy), (ax, ay) = _keep_ellipse(face)
    cv2.ellipse(region, (round(cx), round(cy)), (round(ax), round(ay)), 0, 0, 360, 1.0, -1)
    return region


def _shrink(region, face_width: float):
    import cv2

    radius = max(1, round(face_width * SHRINK))
    return cv2.erode(region, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1)))


class YuNetFaceKeeper:
    """skin: the face-skin probability of every pixel of an image around a face box (FaceParser.face_skin)."""

    def __init__(self, skin: Callable[[Image.Image, tuple], object],
                 detect: Callable[[Image.Image], list[FaceShape]] | None = None):
        self._skin = skin
        self._detect = detect or _yunet_faces()

    def _face_skin(self, image: Image.Image, face: FaceShape):
        try:
            return self._skin(image, face.box) > SKIN
        except FaceParseError as error:
            raise FaceKeepError(error.code, error.message) from None

    def _main_face(self, image: Image.Image) -> FaceShape | None:
        scale = min(1.0, DETECT_LONG_SIDE / max(image.size))
        reduced = image
        if scale < 1.0:
            reduced = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.BILINEAR)
        faces = [FaceShape(tuple(value / scale for value in face.box), tuple((px / scale, py / scale) for px, py in face.points))
                 for face in self._detect(reduced)]
        return max(faces, key=lambda face: face.box[2] * face.box[3], default=None)

    def mask(self, photo: bytes) -> tuple[bytes, FaceShape]:
        """The edit mask (transparent = may change, opaque = keep) and the face it keeps."""
        import cv2
        import numpy as np

        image = _open(photo)
        face = self._main_face(image)
        if face is None:
            raise FaceKeepError(*NO_FACE)
        # Hair inside the ellipse (a fringe) is left free, so the edit may take it away.
        alpha = ((_ellipse(image.size, face) > 0) & self._face_skin(image, face)).astype(np.uint8) * 255
        mask = Image.new('RGBA', image.size, (0, 0, 0, 0))
        mask.putalpha(Image.fromarray(alpha))
        output = io.BytesIO()
        mask.save(output, 'PNG')
        return output.getvalue(), face

    def restore(self, photo: bytes, edited: bytes, face: FaceShape) -> bytes:
        """The edited picture with the photo's face laid back where the edit drew it."""
        import cv2
        import numpy as np

        source, target = _open(photo), _open(edited)
        drawn = self._main_face(target)
        if drawn is None:
            raise FaceKeepError(*FACE_LOST)
        source_points = np.array(face.points, np.float32)
        target_points = np.array(drawn.points, np.float32)
        # Move, turn and scale only: the face keeps its proportions.
        matrix, _ = cv2.estimateAffinePartial2D(source_points, target_points, method=cv2.LMEDS)
        if matrix is None:
            raise FaceKeepError(*FACE_LOST)
        # Five points that do not fit a move-turn-scale mean the edit redrew the face (another expression or
        # angle); laying the photo's face on it would leave a double outline.
        moved = source_points @ matrix[:, :2].T + matrix[:, 2]
        misfit = float(np.linalg.norm(moved - target_points, axis=1).mean()) / drawn.box[2]
        if misfit > MAX_MISFIT:
            raise FaceKeepError(*FACE_LOST)
        # Face skin of the photo (its fringe excluded), moved to where the edit drew the face, and face skin of the
        # edit (a new fringe excluded): only pixels that are face in both are laid back.
        keep = _ellipse(source.size, face) * self._face_skin(source, face).astype(np.float32)
        size = (target.width, target.height)
        moved_keep = cv2.warpAffine(keep, matrix, size, flags=cv2.INTER_NEAREST, borderValue=0)
        both = np.minimum(moved_keep, self._face_skin(target, drawn).astype(np.float32))
        weight = cv2.GaussianBlur(_shrink(both, drawn.box[2]), (0, 0), max(1.0, drawn.box[2] * FEATHER))[..., None]
        laid = cv2.warpAffine(np.asarray(source, np.float32), matrix, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        result = laid * weight + np.asarray(target, np.float32) * (1 - weight)
        output = io.BytesIO()
        Image.fromarray(np.clip(result + 0.5, 0, 255).astype(np.uint8)).save(output, 'JPEG', quality=95, subsampling=0)
        return output.getvalue()
