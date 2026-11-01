"""Which pixels of a face are skin, so hair is never laid back as part of the face (V6.2).

The kept region of a look (app/face_keep.py) was an ellipse; a fringe inside it was laid back over the new
hairstyle as a dark patch on the forehead. A segmentation model now says, pixel by pixel, what is face and what is
hair, in the photo and in the edit.

Model: MediaPipe's multiclass selfie segmenter (Apache 2.0, Google), classes background, hair, body skin, face
skin, clothes, others. OpenCV cannot run it (it lacks transposed convolutions), so it runs on LiteRT
(ai-edge-litert). The file is downloaded on first use into the data folder and checked against a fixed SHA-256.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

MODEL_URL = ('https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_multiclass_256x256/float32/1/'
             'selfie_multiclass_256x256.tflite')
MODEL_SHA256 = 'c6748b1253a99067ef71f7e26ca71096cd449baefa8f101900ea23016507e0e0'
MODEL_FILE = 'selfie_multiclass_256x256.tflite'
INPUT_SIDE = 256
FACE_SKIN = 3  # class index of face skin
# The face is parsed on a square around it, this many face-box sides wide, so it fills the model's 256 px input.
CROP_SIDE = 2.4


class FaceParseError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FaceParser:
    """Face-skin probability of every pixel around a face. Thread-safe; the model loads on first use."""

    def __init__(self, model_dir: Path):
        self._path = Path(model_dir) / MODEL_FILE
        self._lock = threading.Lock()
        self._interpreter = None

    def _download(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        partial = self._path.with_suffix('.part')
        logger.info('Downloading the face segmentation model to %s', self._path)
        try:
            with urllib.request.urlopen(MODEL_URL, timeout=120) as response, open(partial, 'wb') as output:
                digest = hashlib.sha256()
                while chunk := response.read(1 << 20):
                    digest.update(chunk)
                    output.write(chunk)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            logger.warning('Face segmentation model download failed: %s', type(exc).__name__)
            raise FaceParseError('face_model_download_failed',
                                 '人脸分割模型下载失败，请检查这台机器能否访问 storage.googleapis.com 后重试') from None
        if digest.hexdigest() != MODEL_SHA256:
            partial.unlink(missing_ok=True)
            raise FaceParseError('face_model_download_failed', '下载的人脸分割模型校验不通过，请稍后重试')
        os.replace(partial, self._path)

    def _load(self):
        if self._interpreter is not None:
            return self._interpreter
        if not self._path.is_file():
            self._download()
        try:
            from ai_edge_litert.interpreter import Interpreter
        except ImportError:
            raise FaceParseError('face_model_runtime_missing',
                                 '缺少人脸分割的运行库，请在 backend 目录运行 uv pip install --python .venv/bin/python -r requirements.txt 后重启') from None
        interpreter = Interpreter(model_path=str(self._path))
        interpreter.allocate_tensors()
        self._interpreter = interpreter
        return interpreter

    def face_skin(self, image: Image.Image, box: tuple[float, float, float, float]) -> np.ndarray:
        """Probability (0..1) that each pixel of the image is face skin; 0 outside the square parsed around box."""
        x, y, width, height = box
        side = CROP_SIDE * max(width, height)
        centre_x, centre_y = x + width / 2, y + height / 2
        left, top = max(0, round(centre_x - side / 2)), max(0, round(centre_y - side / 2))
        right, bottom = min(image.width, round(centre_x + side / 2)), min(image.height, round(centre_y + side / 2))
        crop = image.crop((left, top, right, bottom)).resize((INPUT_SIDE, INPUT_SIDE), Image.Resampling.BILINEAR)
        tensor = np.asarray(crop, np.float32)[None] / 255.0
        with self._lock:
            interpreter = self._load()
            interpreter.set_tensor(interpreter.get_input_details()[0]['index'], tensor)
            interpreter.invoke()
            logits = interpreter.get_tensor(interpreter.get_output_details()[0]['index'])[0]
        logits = logits - logits.max(axis=-1, keepdims=True)
        probabilities = np.exp(logits)
        face = probabilities[..., FACE_SKIN] / probabilities.sum(axis=-1)
        resized = np.asarray(Image.fromarray(face.astype(np.float32)).resize(
            (right - left, bottom - top), Image.Resampling.BILINEAR))
        result = np.zeros((image.height, image.width), np.float32)
        result[top:bottom, left:right] = resized
        return result
