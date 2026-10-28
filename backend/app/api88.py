"""88API image editing with conservative, billing-aware outcome classification."""

import base64
import binascii
import json
import logging
import math
import mimetypes
import re
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .config import Config
from .generator import GeneratorError, ImageGenerator, OutcomeKind
from .models import get_model

logger = logging.getLogger(__name__)

# Keep the recommended dimensions identical to image-gen/scripts/gen_image.py.
OPENAI_SIZES = {
    '1:1': {'1K': '1024x1024', '2K': '2048x2048', '4K': '2880x2880'},
    '3:2': {'1K': '1536x1024', '2K': '2400x1600', '4K': '3504x2336'},
    '2:3': {'1K': '1024x1536', '2K': '1600x2400', '4K': '2336x3504'},
    '4:3': {'1K': '1152x864', '2K': '2048x1536', '4K': '3264x2448'},
    '3:4': {'1K': '864x1152', '2K': '1536x2048', '4K': '2448x3264'},
    '16:9': {'1K': '1280x720', '2K': '2560x1440', '4K': '3840x2160'},
    '9:16': {'1K': '720x1280', '2K': '1440x2560', '4K': '2160x3840'},
    '21:9': {'1K': '1344x576', '2K': '3024x1296', '4K': '3840x1648'},
    '2:1': {'1K': '1408x704', '2K': '2880x1440', '4K': '3840x1920'},
    '1:2': {'1K': '704x1408', '2K': '1440x2880', '4K': '1920x3840'},
}
GEMINI_RATIOS = {'1:1', '2:3', '3:2', '3:4', '4:3', '4:5', '5:4', '9:16', '16:9', '21:9'}
SUPPORTED_RATIOS = set(OPENAI_SIZES) & GEMINI_RATIOS

AUTH_MESSAGE = 'API Key 无效或账户余额不足，请检查模型设置后点击「继续剩余」'
REJECTED_MESSAGE = '请求未通过模型服务审核或参数无效，请更换照片或稍后重试'
SERVICE_MESSAGE = '模型服务暂时异常，请稍后重试'
NO_IMAGE_MESSAGE = '模型没有返回图片，请重试'
UNKNOWN_OUTCOME_MESSAGE = '模型服务连接中断，结果无法确认，可能已经扣费。重新生成可能产生额外费用'
DOWNLOAD_FAILED_MESSAGE = '图片已生成但下载失败，可能已经扣费。重新生成可能产生额外费用'
QUOTA_PATTERN = re.compile(r'quota|balance|insufficient|余额|配额|额度|欠费', re.IGNORECASE)


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class Api88Generator(ImageGenerator):
    def __init__(self, config: Config, *, transport: httpx.BaseTransport | None = None):
        if not isinstance(config.api_key, str) or not config.api_key.strip():
            raise ValueError('使用 88API 时必须在本地配置文件中填写非空 api_key')
        if get_model(config.model) is None:
            raise ValueError('暂不支持此模型，请使用模型列表中的模型')
        if config.aspect_ratio not in SUPPORTED_RATIOS:
            raise ValueError('此模型不支持配置的 aspect_ratio，请检查图片比例')
        if config.resolution not in ('1K', '2K', '4K'):
            raise ValueError('resolution 必须是 1K、2K 或 4K')
        if not isinstance(config.timeout, (int, float)) or not math.isfinite(config.timeout) or config.timeout <= 0:
            raise ValueError('timeout 必须是大于零的有限秒数')
        try:
            url = urlsplit(config.base_url)
            valid_url = url.scheme in ('http', 'https') and url.hostname and not (url.username or url.password or url.query or url.fragment)
        except (TypeError, ValueError):
            valid_url = False
        if not valid_url:
            raise ValueError('base_url 必须是有效的 HTTP(S) 地址，不能包含凭据、查询参数或片段')
        self._config = config
        self._transport = transport

    def _error_detail(self, response: httpx.Response) -> str:
        try:
            # Decode escaped Unicode too, so Chinese quota messages are detected.
            return json.dumps(response.json(), ensure_ascii=False)
        except ValueError:
            return response.text

    def _log_provider_error(self, response: httpx.Response):
        # Providers may echo credentials in their errors. Redact before creating
        # a LogRecord (including args); never log headers or exception objects.
        detail = self._error_detail(response).replace(self._config.api_key, '[REDACTED]')
        escaped_key = json.dumps(self._config.api_key)[1:-1]
        detail = detail.replace(escaped_key, '[REDACTED]')
        logger.warning('88API HTTP %s: %s', response.status_code, detail[:2000])

    def _redact(self, value: object) -> str:
        text = str(value)
        text = text.replace(self._config.api_key, '[REDACTED]')
        escaped_key = json.dumps(self._config.api_key)[1:-1]
        return text.replace(escaped_key, '[REDACTED]')

    def _transport_error(self, exc: Exception, *, model: str, phase: str, started: float, url: str | None = None):
        fields = {
            'phase': phase,
            'model': model,
            'elapsed_seconds': f'{time.monotonic() - started:.3f}',
            'exception_class': type(exc).__name__,
            'error': self._redact(exc),
        }
        if url is not None:
            fields['url_host'] = urlsplit(url).hostname or ''
        logger.warning('88API transport failure %s', ' '.join(f'{key}={value}' for key, value in fields.items()))

    def _check_request_url(self, request: httpx.Request):
        # HTTPX logs request URLs, including redirect targets. Stop any echoed
        # credential before a request or its LogRecord can be created.
        if self._config.api_key in unquote(str(request.url)):
            raise GeneratorError(OutcomeKind.UNKNOWN_OUTCOME)

    def _request(self, client: httpx.Client, method: str, url: str, *, model: str, download: bool = False, **kwargs) -> httpx.Response:
        started = time.monotonic()
        phase = 'download' if download else 'generation'
        try:
            response = client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, socket.gaierror) as exc:
            # Only connection establishment proves the paid generation request
            # was not sent. Never apply this safe-retry rule to a later download:
            # generation already succeeded, so resubmitting could charge twice.
            self._transport_error(exc, model=model, phase=phase, started=started, url=url if download else None)
            if download:
                raise GeneratorError(OutcomeKind.UNKNOWN_OUTCOME, DOWNLOAD_FAILED_MESSAGE, error_code='download_failed') from None
            raise GeneratorError(OutcomeKind.NOT_SENT) from None
        except (httpx.RequestError, httpx.StreamError, httpx.InvalidURL, OSError) as exc:
            # Read/write/protocol failures can occur after the server received
            # the request. Treat ambiguity as interrupted, never auto-resubmit.
            self._transport_error(exc, model=model, phase=phase, started=started, url=url if download else None)
            raise GeneratorError(OutcomeKind.UNKNOWN_OUTCOME,
                                 DOWNLOAD_FAILED_MESSAGE if download else UNKNOWN_OUTCOME_MESSAGE,
                                 error_code='download_failed' if download else 'unknown_outcome') from None

        if not response.is_success:
            self._log_provider_error(response)
            if download:
                raise GeneratorError(OutcomeKind.UNKNOWN_OUTCOME, DOWNLOAD_FAILED_MESSAGE, error_code='download_failed')
            if response.status_code in (401, 402, 403):
                raise GeneratorError(OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE)
            if response.status_code == 429:
                raise GeneratorError(OutcomeKind.RATE_LIMITED, retry_after=retry_after_seconds(response.headers.get('Retry-After')))
            if QUOTA_PATTERN.search(self._error_detail(response)):
                raise GeneratorError(OutcomeKind.AUTH_OR_QUOTA, AUTH_MESSAGE)
            if response.status_code in (502, 504):
                # The relay lost contact; upstream generation may still be billing.
                # Treat the result as unknown, never auto-resubmit.
                raise GeneratorError(OutcomeKind.UNKNOWN_OUTCOME,
                                     '模型服务响应超时，结果无法确认，可能已经扣费。重新生成可能产生额外费用')
            message = SERVICE_MESSAGE if response.status_code >= 500 else REJECTED_MESSAGE
            raise GeneratorError(OutcomeKind.REJECTED, message)
        return response

    def generate_image(self, images: list[Path], prompt: str, model: str, aspect_ratio: str) -> bytes:
        if aspect_ratio not in SUPPORTED_RATIOS:
            raise ValueError(f'unsupported aspect_ratio {aspect_ratio}')
        model_id = model
        model_definition = get_model(model_id)
        if model_definition is None:
            raise ValueError('暂不支持此模型，请使用模型列表中的模型')
        family = model_definition.family
        # Read every input before opening a connection, preserving bride-first
        # ordering supplied by the task's immutable reference-image snapshot.
        try:
            references = [(path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or 'image/png') for path in images]
        except OSError:
            raise GeneratorError(OutcomeKind.NOT_SENT, '无法读取参考照片，请检查文件后重试') from None
        if not references:
            raise GeneratorError(OutcomeKind.REJECTED, '请先上传参考照片')

        # A call owns its client, so concurrent worker threads share no client
        # lifecycle and shutdown needs no additional resource-management hooks.
        with httpx.Client(transport=self._transport, timeout=self._config.timeout, follow_redirects=False,
                          trust_env=False, event_hooks={'request': [self._check_request_url]}) as client:
            headers = {'Authorization': f'Bearer {self._config.api_key}', 'User-Agent': 'vowframe-image-gen/1.0'}
            base_url = self._config.base_url.rstrip('/')
            if family == 'openai':
                response = self._request(
                    client, 'POST', base_url + '/v1/images/edits', headers=headers,
                    model=model_id,
                    data={'model': model_id, 'prompt': prompt,
                          'size': OPENAI_SIZES[aspect_ratio][self._config.resolution], 'n': '1'},
                    files=[('image[]', reference) for reference in references],
                )
            else:
                content = [{'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{base64.b64encode(data).decode()}'}}
                           for _, data, mime in references]
                content.append({'type': 'text', 'text': prompt})
                response = self._request(
                    client, 'POST', base_url + '/v1/chat/completions', headers=headers,
                          model=model_id,
                          json={'model': model_id, 'messages': [{'role': 'user', 'content': content}],
                          'extra_body': {'google': {'image_config': {
                              'aspect_ratio': aspect_ratio, 'image_size': self._config.resolution}}}},
                )

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
            if family == 'gemini':
                payload = self._gemini_images(payload)
            return self._image_bytes(client, payload, model_id)

    def _gemini_images(self, payload: dict) -> dict:
        try:
            text = payload['choices'][0]['message'].get('content') or ''
        except (KeyError, IndexError, TypeError, AttributeError):
            text = ''
        if not isinstance(text, str):
            text = ''
        # Match run_gemini: inline base64 images take precedence over remote URLs.
        images = [{'data_uri': uri} for uri in re.findall(r'\((data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+)\)', text)]
        images.extend({'url': url} for url in re.findall(r'!\[[^\]]*\]\((https?://[^)\s]+)\)', text))
        return {'data': images}

    def _image_bytes(self, client: httpx.Client, payload: dict, model: str) -> bytes:
        items = payload.get('data')
        if not isinstance(items, list):
            raise GeneratorError(OutcomeKind.REJECTED, NO_IMAGE_MESSAGE)
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                if item.get('b64_json'):
                    data = base64.b64decode(item['b64_json'])
                elif item.get('data_uri'):
                    data = base64.b64decode(item['data_uri'].split(',', 1)[1])
                elif item.get('url'):
                    url = item['url']
                    if not isinstance(url, str) or not url.startswith(('https://', 'http://')) or self._config.api_key in url:
                        raise GeneratorError(OutcomeKind.UNKNOWN_OUTCOME, DOWNLOAD_FAILED_MESSAGE, error_code='download_failed')
                    # Never forward the API Authorization header to image hosts.
                    try:
                        data = self._request(client, 'GET', url, model=model, download=True, follow_redirects=True).content
                    except GeneratorError as error:
                        if error.error_code == 'download_failed':
                            raise
                        raise GeneratorError(OutcomeKind.UNKNOWN_OUTCOME, DOWNLOAD_FAILED_MESSAGE, error_code='download_failed') from None
                    except Exception as exc:
                        self._transport_error(exc, model=model, phase='download', started=time.monotonic(), url=url)
                        raise GeneratorError(OutcomeKind.UNKNOWN_OUTCOME, DOWNLOAD_FAILED_MESSAGE, error_code='download_failed') from None
                    if not data:
                        raise GeneratorError(OutcomeKind.UNKNOWN_OUTCOME, DOWNLOAD_FAILED_MESSAGE, error_code='download_failed')
                else:
                    continue
            except (ValueError, TypeError, IndexError, AttributeError, binascii.Error):
                raise GeneratorError(OutcomeKind.REJECTED, NO_IMAGE_MESSAGE) from None
            if data:
                return data
        raise GeneratorError(OutcomeKind.REJECTED, NO_IMAGE_MESSAGE)
