"""Provider boundary with explicit request-outcome semantics."""

import time
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class OutcomeKind(StrEnum):
    NOT_SENT = 'not_sent'
    REJECTED = 'rejected'
    AUTH_OR_QUOTA = 'auth_or_quota'
    RATE_LIMITED = 'rate_limited'
    UNKNOWN_OUTCOME = 'unknown_outcome'


ERROR_MESSAGES = {
    OutcomeKind.NOT_SENT: '连接生成服务失败，请检查网络后重试',
    OutcomeKind.REJECTED: '内容审核未通过，请调整照片后重试',
    OutcomeKind.AUTH_OR_QUOTA: '鉴权失败或余额不足，请检查模型设置和账户余额后继续',
    OutcomeKind.RATE_LIMITED: '生成服务请求过于频繁，正在等待后重试',
    OutcomeKind.UNKNOWN_OUTCOME: '上次请求的结果无法确认，你可以重新发起生成。重新请求可能产生额外费用。',
    'interrupted_by_restart': '后端重启时这次请求还在进行，结果无法确认，可能已经扣费。重新生成可能产生额外费用',
    'unknown_outcome': '模型服务连接中断，结果无法确认，可能已经扣费。重新生成可能产生额外费用',
    'download_failed': '图片已生成但下载失败，可能已经扣费。重新生成可能产生额外费用',
}


class GeneratorError(Exception):
    def __init__(self, kind: OutcomeKind | str, message: str | None = None, *, retry_after: float | None = None,
                 error_code: str | None = None):
        self.kind = OutcomeKind(kind)
        self.error_code = error_code or self.kind.value
        self.code = self.error_code
        self.message = message or ERROR_MESSAGES.get(self.error_code, ERROR_MESSAGES[self.kind])
        self.retry_after = retry_after
        super().__init__(self.message)


class ImageGenerator(Protocol):
    def generate_image(self, images: list[Path], prompt: str, model: str, aspect_ratio: str) -> bytes: ...


class MockGenerator:
    def __init__(self, delay_seconds: float = 2):
        self.delay_seconds = delay_seconds

    def generate_image(self, images: list[Path], prompt: str, model: str, aspect_ratio: str) -> bytes:
        time.sleep(self.delay_seconds)
        return images[0].read_bytes()
