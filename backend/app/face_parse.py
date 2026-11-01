"""Which pixels of a face are face, skin or hair (V6.2, SegFace since V6.3).

V6.2–V6.4 used it to lay the uploaded face back over a look without its fringe. Since V7.1 the AI face fix
(app/face_fix.py) lays only face pixels of an edited crop back into a picture, never hair, hands or headwear.

Model: SegFace (MIT, github.com/Kartik-3004/SegFace) with the MobileNetV3 backbone, CelebAMask-HQ weights, exported
to ONNX and shipped in app/assets (V6.3). Compared on the project's photos, it marks fringe strands lying on the
forehead as hair where MediaPipe's selfie segmenter (V6.2) took some of them for face, and its hairline is smooth
(512 px input instead of 256). It also tells brows, eyes, nose and lips apart. The weights were trained on
CelebAMask-HQ, whose terms allow non-commercial research use only; the project is for personal use
(app/assets/face_parsing_segface_LICENSE).
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from PIL import Image

MODEL_PATH = Path(__file__).parent / 'assets' / 'face_parsing_segface_mobilenet_512.onnx'
INPUT_SIDE = 512
MEAN = np.array([0.485, 0.456, 0.406], np.float32).reshape(1, 3, 1, 1)
STD = np.array([0.229, 0.224, 0.225], np.float32).reshape(1, 3, 1, 1)
# CelebAMask-HQ classes: 0 background, 1 neck, 2 skin, 3 cloth, 4-5 ears, 6-7 brows, 8-9 eyes, 10 nose,
# 11 inner mouth, 12-13 lips, 14 hair, 15 glasses, 16 hat, 17 earring, 18 necklace. The face is skin and the
# features; ears are left out, a new hairstyle may cover them.
FACE_CLASSES = (2, 6, 7, 8, 9, 10, 11, 12, 13)
# The face is parsed on a square around it, this many face-box sides wide: about the framing of CelebAMask-HQ,
# a whole head with some neck.
CROP_SIDE = 2.4


class FaceParseError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FaceParser:
    """Face probability of every pixel around a face. Thread-safe; the model loads on first use."""

    def __init__(self, model_path: Path = MODEL_PATH):
        self._path = Path(model_path)
        self._lock = threading.Lock()
        self._session = None

    def _load(self):
        if self._session is not None:
            return self._session
        try:
            import onnxruntime
        except ImportError:
            raise FaceParseError('face_model_runtime_missing',
                                 '缺少人脸分割的运行库，请在 backend 目录运行 uv pip install --python .venv/bin/python -r requirements.txt 后重启') from None
        if not self._path.is_file():
            raise FaceParseError('face_model_missing', f'人脸分割模型文件缺失：{self._path.name}，请拉取完整的代码后重启')
        self._session = onnxruntime.InferenceSession(str(self._path), providers=['CPUExecutionProvider'])
        return self._session

    def face_skin(self, image: Image.Image, box: tuple[float, float, float, float]) -> np.ndarray:
        """Probability (0..1) that each pixel of the image is face (skin, brows, eyes, nose, mouth, lips); 0 outside
        the square parsed around box."""
        x, y, width, height = box
        side = CROP_SIDE * max(width, height)
        centre_x, centre_y = x + width / 2, y + height / 2
        left, top = max(0, round(centre_x - side / 2)), max(0, round(centre_y - side / 2))
        right, bottom = min(image.width, round(centre_x + side / 2)), min(image.height, round(centre_y + side / 2))
        crop = image.crop((left, top, right, bottom)).resize((INPUT_SIDE, INPUT_SIDE), Image.Resampling.BICUBIC)
        tensor = ((np.asarray(crop, np.float32) / 255.0).transpose(2, 0, 1)[None] - MEAN) / STD
        with self._lock:
            session = self._load()
            probabilities = session.run(None, {'image': tensor.astype(np.float32)})[0][0]
        face = probabilities[list(FACE_CLASSES)].sum(axis=0).astype(np.float32)
        resized = np.asarray(Image.fromarray(face).resize((right - left, bottom - top), Image.Resampling.BILINEAR))
        result = np.zeros((image.height, image.width), np.float32)
        result[top:bottom, left:right] = resized
        return result
