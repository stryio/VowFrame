"""火山方舟 (Volcengine Ark): the official Seedream image API (2026-11-13).

Ark has one JSON endpoint, /images/generations, for both text-to-image and edits: the references go in `image` as
data URIs and the size is exact pixels. Everything after the request is shared with 88API (Api88Generator): the
billing-aware outcome classification, key redaction and reading the returned picture, so the two services fail the
same way and a paid request is never silently repeated.
"""

import base64
import logging
from pathlib import Path

import httpx
from PIL import Image

from .api88 import OPENAI_SIZES, SUPPORTED_RATIOS, Api88Generator
from .generator import GeneratorError, OutcomeKind
from .regional_edit import RegionalEdit
from .storage import outgoing_image

logger = logging.getLogger(__name__)

# Ark's documented range for a picture it makes (Seedream 5.0 pro page; the 5.5 page gives no other): total pixels
# width x height within [921600, 4624220]. 4K is above it, so a wash cannot use Seedream.
MIN_PIXELS, MAX_PIXELS = 921_600, 4_624_220
TOO_LARGE_MESSAGE = '火山方舟 Seedream 最高只出 2K 图，不能用于 4K 洗图；请在设置里给洗图换一个模型'


def ark_size(aspect_ratio: str, resolution: str) -> str:
    """The pixels asked of Ark: the same table as the GPT models, so size_problem can check what came back."""
    size = OPENAI_SIZES[aspect_ratio][resolution]
    width, height = (int(value) for value in size.split('x'))
    if width * height > MAX_PIXELS:
        raise GeneratorError(OutcomeKind.NOT_SENT, TOO_LARGE_MESSAGE, error_code='size_unsupported')
    if width * height < MIN_PIXELS:
        raise GeneratorError(OutcomeKind.NOT_SENT, f'火山方舟 Seedream 不支持 {aspect_ratio} 比例的 {resolution} 图片，请改用 2K',
                             error_code='size_unsupported')
    return size


class ArkGenerator(Api88Generator):
    """Seedream on 火山方舟. config.base_url is Ark's API root (…/api/v3) and config.api_key its API Key."""

    def __init__(self, config, *, transport=None, provider: str = '火山方舟'):
        super().__init__(config, transport=transport, provider=provider)

    def generate_image(self, images: list[Path], prompt: str, model: str, aspect_ratio: str,
                       resolution: str | None = None, edit: RegionalEdit | None = None) -> bytes:
        if aspect_ratio not in SUPPORTED_RATIOS:
            raise ValueError(f'unsupported aspect_ratio {aspect_ratio}')
        resolution = resolution or self._config.resolution
        if resolution not in ('1K', '2K', '4K'):
            raise ValueError(f'unsupported resolution {resolution}')
        if edit is not None:
            # Only the whole-picture methods pass an edit; Seedream does the crop edit (two plain pictures, no edit).
            raise GeneratorError(OutcomeKind.REJECTED, '此模型不支持局部标记编辑，请选择 GPT Image 2.5 或 2.6')
        size = ark_size(aspect_ratio, resolution)
        # Read every input before opening a connection; photos leave without their metadata.
        try:
            references = [outgoing_image(path) for path in images]
        except (OSError, ValueError, Image.DecompressionBombError):
            raise GeneratorError(OutcomeKind.NOT_SENT, '无法读取参考照片，请检查文件后重试') from None

        body = {'model': model, 'prompt': prompt, 'size': size, 'response_format': 'b64_json',
                'output_format': 'png', 'watermark': False}
        if references:
            # Ark wants the image type in lower case inside the data URI.
            body['image'] = [f'data:{mime.lower()};base64,{base64.b64encode(data).decode()}' for _, data, mime in references]
        with httpx.Client(transport=self._transport, timeout=self._config.timeout, follow_redirects=False,
                          trust_env=False, event_hooks={'request': [self._check_request_url]}) as client:
            headers = {'Authorization': f'Bearer {self._config.api_key}', 'User-Agent': 'vowframe-image-gen/1.0'}
            response = self._request(client, 'POST', self._config.base_url.rstrip('/') + '/images/generations',
                                     headers=headers, model=model, json=body)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                raise GeneratorError(OutcomeKind.REJECTED, '模型没有返回图片，请重试')
            if payload.get('error'):
                self._log_provider_error(response)
                raise GeneratorError(OutcomeKind.REJECTED, '请求未通过模型服务审核或参数无效，请更换照片或稍后重试')
            items = payload.get('data')
            if isinstance(items, list) and any(isinstance(item, dict) and item.get('error') for item in items):
                # A picture Ark refused (for example its own content check) comes back as an item with an error.
                self._log_provider_error(response)
            return self._image_bytes(client, payload, model)
