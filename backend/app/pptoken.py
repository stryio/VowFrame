"""PPTOKEN image edits with a mask, for front looks (V6.0).

88API does not take a mask on its edits endpoint, so the one edit that needs one goes to PPTOKEN's gpt-image-2.5.
The request and outcome rules are 88API's (app.api88): only the endpoint, the key and the request fields differ.
"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
from PIL import Image

from .api88 import NO_IMAGE_MESSAGE, QUOTA_PATTERN, AUTH_MESSAGE, REJECTED_MESSAGE, Api88Generator
from .config import Config
from .generator import GeneratorError, OutcomeKind
from .storage import outgoing_image

MASK_MODEL = 'gpt-image-2.5'
# PPTOKEN takes only a few fixed sizes and none of them is 3:4; 'auto' follows the photo, and the face is laid
# back onto whatever size comes back (app/face_keep.py).
MASK_SIZE = 'auto'


class PptokenMaskEditor(Api88Generator):
    PROVIDER = 'PPTOKEN'
    mask_model = MASK_MODEL

    def __init__(self, config: Config, *, transport: httpx.BaseTransport | None = None):
        super().__init__(replace(config, base_url=config.pptoken_base_url, api_key=config.pptoken_api_key), transport=transport)

    def edit_with_mask(self, image: Path, mask: bytes | None, prompt: str) -> bytes:
        try:
            reference = outgoing_image(image)
        except (OSError, ValueError, Image.DecompressionBombError):
            raise GeneratorError(OutcomeKind.NOT_SENT, '无法读取参考照片，请检查文件后重试') from None
        files = [('image', reference)]
        if mask is not None:
            files.append(('mask', ('mask.png', mask, 'image/png')))
        with httpx.Client(transport=self._transport, timeout=self._config.timeout, follow_redirects=False,
                          trust_env=False, event_hooks={'request': [self._check_request_url]}) as client:
            headers = {'Authorization': f'Bearer {self._config.api_key}', 'User-Agent': 'vowframe-image-gen/1.0'}
            response = self._request(
                client, 'POST', self._config.base_url.rstrip('/') + '/v1/images/edits', headers=headers, model=MASK_MODEL,
                data={'model': MASK_MODEL, 'prompt': prompt, 'size': MASK_SIZE, 'quality': 'high'}, files=files)
            try:
                payload = response.json()
            except ValueError:
                raise GeneratorError(OutcomeKind.REJECTED, NO_IMAGE_MESSAGE) from None
            if not isinstance(payload, dict):
                raise GeneratorError(OutcomeKind.REJECTED, NO_IMAGE_MESSAGE)
            if payload.get('error'):
                self._log_provider_error(response)
                if QUOTA_PATTERN.search(json.dumps(payload['error'], ensure_ascii=False)):
                    raise GeneratorError(OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE)
                raise GeneratorError(OutcomeKind.REJECTED, REJECTED_MESSAGE)
            return self._image_bytes(client, payload, MASK_MODEL)
