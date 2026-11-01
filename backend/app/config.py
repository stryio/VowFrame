"""Local model settings with environment overrides for runtime configuration."""

import json
import logging
import os
import math
from dataclasses import dataclass, field
from pathlib import Path

from .models import DEFAULT_MODEL, MODEL_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    max_concurrency: int
    generator: str
    mock_delay_seconds: float
    api_key: str = field(default='', repr=False)
    base_url: str = 'https://api.88api.ai'
    model: str = 'gpt-image-2'
    timeout: float = 600
    aspect_ratio: str = '3:4'
    resolution: str = '2K'
    director_model: str | None = None
    facefusion_dir: Path | None = None

    def __post_init__(self):
        object.__setattr__(self, 'data_dir', Path(self.data_dir).resolve())
        if self.facefusion_dir is not None:
            object.__setattr__(self, 'facefusion_dir', Path(self.facefusion_dir).expanduser().resolve())
        if self.max_concurrency < 1:
            raise ValueError('max_concurrency must be at least 1')
        if not math.isfinite(self.mock_delay_seconds) or self.mock_delay_seconds < 0:
            raise ValueError('mock_delay_seconds must be finite and non-negative')
        if self.generator == '88api' and (not isinstance(self.api_key, str) or not self.api_key.strip()):
            raise ValueError('使用 88API 时必须在本地配置文件中填写非空 api_key')

    @property
    def model_config_snapshot(self) -> dict:
        # Explicit allowlist: never serialize the configuration object or API key.
        return {
            'generator': self.generator,
            'model': self.model,
            'base_url': self.base_url,
            'aspect_ratio': self.aspect_ratio,
            'resolution': self.resolution,
        }

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vowframe.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "temp"


def load_config() -> Config:
    path = Path(os.environ.get('VOWFRAME_LOCAL_CONFIG', REPO_ROOT / 'backend' / 'config.local.json'))
    try:
        local = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        local = {}
    except (OSError, ValueError):
        # Parser errors can include source text containing the secret.
        raise ValueError('无法读取本地模型配置，请检查文件权限及 JSON 格式') from None
    if not isinstance(local, dict):
        raise ValueError('本地模型配置必须是 JSON 对象')
    configured_model = local.get('model', DEFAULT_MODEL)
    if not isinstance(configured_model, str) or configured_model not in {model.id for model in MODEL_REGISTRY}:
        logger.warning('本地配置中的模型不受支持，已回退到默认模型')
        configured_model = DEFAULT_MODEL
    director_model = local.get('director_model')
    if not isinstance(director_model, str) or not director_model.strip():
        director_model = None
    facefusion_dir = local.get('facefusion_dir')
    if not isinstance(facefusion_dir, str) or not facefusion_dir.strip():
        facefusion_dir = None
    return Config(
        data_dir=Path(os.environ.get("VOWFRAME_DATA_DIR", REPO_ROOT / "data")),
        max_concurrency=int(os.environ.get("VOWFRAME_MAX_CONCURRENCY", "2")),
        generator=os.environ.get("VOWFRAME_GENERATOR", local.get('generator', 'mock')),
        mock_delay_seconds=float(os.environ.get("VOWFRAME_MOCK_DELAY", "2")),
        **{key: local[key] for key in ('api_key', 'base_url', 'timeout', 'aspect_ratio', 'resolution') if key in local},
        director_model=director_model,
        model=configured_model,
        facefusion_dir=facefusion_dir,
    )
