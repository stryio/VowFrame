"""Local face-swap provider boundary and isolated FaceFusion process adapter."""

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from .config import Config

FACE_SWAP_MESSAGES = {
    'roles_swapped': '新娘和新郎的照片可能放反了，请在工作台点『交换』后重试',
    'face_swap_failed': '换脸处理失败，请稍后重试',
}
FACE_NOT_FOUND_MESSAGES = {
    'bride': '没有识别到新娘的脸，可能是侧脸角度过大或被遮挡',
    'groom': '没有识别到新郎的脸，可能是侧脸角度过大或被遮挡',
    'both': '没有识别到新娘的脸，可能是侧脸角度过大或被遮挡；没有识别到新郎的脸，可能是侧脸角度过大或被遮挡',
}
HELPER_PATH = Path(__file__).resolve().parents[1] / 'facefusion_helper' / 'swap.py'
# 0.5 blends the reference face into the generated one: closer to the couple than no swap, while keeping
# more of the generated expression and lighting than a full 1.0 replacement.
FACE_SWAP_WEIGHT = 0.5
# Samples are sent to the image model as the third reference, so their faces should be as close to the couple
# as possible; the neutral expression a full swap brings does not matter there (docs/PRD.md 4.5).
PREVIEW_FACE_SWAP_WEIGHT = 1.0


def face_swap_weight(slot_kind: str) -> float:
    return PREVIEW_FACE_SWAP_WEIGHT if slot_kind == 'preview' else FACE_SWAP_WEIGHT
logger = logging.getLogger(__name__)


class FaceSwapError(Exception):
    def __init__(self, error_code: str, message: str | None = None):
        if error_code not in ('roles_swapped', 'face_not_found', 'face_swap_failed'):
            raise ValueError('Unsupported face-swap error code')
        self.error_code = error_code
        self.code = error_code
        if message is None:
            message = FACE_SWAP_MESSAGES.get(error_code, FACE_NOT_FOUND_MESSAGES['both'])
        self.message = message
        super().__init__(message)


class FaceSwapper(Protocol):
    @property
    def status(self) -> dict: ...

    def swap(self, bride: Path, groom: Path, target: Path, weight: float) -> bytes: ...


class MockFaceSwapper:
    @property
    def status(self) -> dict:
        return {'available': True, 'mode': 'mock', 'reason': None}

    def swap(self, bride: Path, groom: Path, target: Path, weight: float) -> bytes:
        return target.read_bytes()


class UnavailableFaceSwapper:
    def __init__(self, reason: str):
        self.reason = reason

    @property
    def status(self) -> dict:
        return {'available': False, 'mode': None, 'reason': self.reason}

    def swap(self, bride: Path, groom: Path, target: Path, weight: float) -> bytes:
        raise FaceSwapError('face_swap_failed')


class FaceFusionSwapper:
    timeout_seconds = 120

    def __init__(self, facefusion_dir: Path, helper_path: Path = HELPER_PATH):
        self.facefusion_dir = Path(facefusion_dir).expanduser().resolve()
        self.python_path = self.facefusion_dir / '.venv' / 'bin' / 'python'
        self.helper_path = Path(helper_path).resolve()

    @property
    def status(self) -> dict:
        if not self.facefusion_dir.joinpath('run.sh').is_file() or not self.python_path.is_file():
            return {
                'available': False,
                'mode': None,
                'reason': 'FaceFusion 安装目录不完整，须包含 run.sh 和 .venv/bin/python',
            }
        if not self.helper_path.is_file():
            return {'available': False, 'mode': None, 'reason': '换脸辅助脚本不存在，请检查后端安装'}
        return {'available': True, 'mode': 'facefusion', 'reason': None}

    def swap(self, bride: Path, groom: Path, target: Path, weight: float) -> bytes:
        if not self.status['available']:
            raise FaceSwapError('face_swap_failed')
        with tempfile.TemporaryDirectory(prefix='vowframe-face-swap-') as directory:
            output = Path(directory) / 'result.png'
            payload = {
                'facefusion_dir': str(self.facefusion_dir),
                'bride_path': str(Path(bride).resolve()),
                'groom_path': str(Path(groom).resolve()),
                'target_path': str(Path(target).resolve()),
                'output_path': str(output),
                'work_dir': directory,
                'weight': weight,
            }
            environment = os.environ.copy()
            site_packages = self.facefusion_dir / '.venv' / 'lib'
            python_sites = sorted(
                site_packages.glob('python*/site-packages/nvidia'),
                key=lambda path: tuple(int(part) for part in path.parts[-3].removeprefix('python').split('.')),
            )
            cuda_library_dirs = []
            if python_sites:
                cuda_library_dirs.extend(
                    str(candidate) for candidate in (
                        python_sites[-1] / 'cu13' / 'lib',
                        python_sites[-1] / 'cudnn' / 'lib',
                    ) if candidate.is_dir()
                )
            if Path('/usr/lib/wsl/lib').is_dir():
                cuda_library_dirs.append('/usr/lib/wsl/lib')
            inherited = environment.get('LD_LIBRARY_PATH', '')
            environment['LD_LIBRARY_PATH'] = os.pathsep.join(
                cuda_library_dirs + ([inherited] if inherited else [])
            )
            environment['PYTHONPATH'] = os.pathsep.join(
                [str(self.facefusion_dir), environment.get('PYTHONPATH', '')]
            ).rstrip(os.pathsep)
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            try:
                completed = subprocess.run(
                    [str(self.python_path), str(self.helper_path)],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    cwd=self.facefusion_dir,
                    env=environment,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                logger.warning('FaceFusion helper timed out after %s seconds', self.timeout_seconds)
                raise FaceSwapError('face_swap_failed') from None
            except OSError as exc:
                logger.warning('FaceFusion helper could not start (%s)', type(exc).__name__)
                raise FaceSwapError('face_swap_failed') from None
            try:
                result = json.loads(completed.stdout)
            except (TypeError, ValueError):
                logger.warning('FaceFusion helper returned invalid JSON (exit_status=%s)', completed.returncode)
                raise FaceSwapError('face_swap_failed') from None
            if completed.returncode != 0 or not isinstance(result, dict) or not result.get('ok'):
                code = result.get('error_code') if isinstance(result, dict) else None
                if code == 'roles_swapped':
                    raise FaceSwapError(code)
                if code == 'face_not_found':
                    missing = result.get('missing', [])
                    missing = set(missing) if isinstance(missing, list) else set()
                    who = 'both' if {'bride', 'groom'} <= missing else 'bride' if 'bride' in missing else 'groom'
                    raise FaceSwapError(code, FACE_NOT_FOUND_MESSAGES[who])
                diagnostic = result.get('diagnostic') if isinstance(result, dict) else None
                logger.warning('FaceFusion helper failed (exit_status=%s, diagnostic=%s)',
                               completed.returncode, diagnostic if isinstance(diagnostic, str) else 'unavailable')
                raise FaceSwapError('face_swap_failed')
            if not result.get('cuda_face_swapper_used'):
                logger.warning('FaceFusion profile did not confirm CUDAExecutionProvider for the face-swap model')
                raise FaceSwapError('face_swap_failed')
            if not output.is_file():
                logger.warning('FaceFusion did not create an output image')
                raise FaceSwapError('face_swap_failed')
            try:
                return output.read_bytes()
            except OSError:
                logger.warning('FaceFusion output could not be read')
                raise FaceSwapError('face_swap_failed') from None


def create_face_swapper(config: Config) -> FaceSwapper:
    selected = os.environ.get('VOWFRAME_FACE_SWAPPER')
    if selected == 'mock':
        return MockFaceSwapper()
    if selected:
        raise ValueError('VOWFRAME_FACE_SWAPPER 只支持 mock')
    if config.facefusion_dir is None:
        return UnavailableFaceSwapper('未配置 FaceFusion 安装目录')
    swapper = FaceFusionSwapper(config.facefusion_dir)
    if not swapper.status['available']:
        return UnavailableFaceSwapper(swapper.status['reason'])
    return swapper


def face_swap_snapshot(mode: str, weight: float) -> str:
    """Model record stored on face-swap tasks; never contains secrets."""
    return json.dumps({
        'generator': mode,
        'model': 'hyperswap_1a_256/hyperswap_1c_256',
        'bride_model': 'hyperswap_1a_256',
        'groom_model': 'hyperswap_1c_256',
        'pixel_boost': '512x512',
        'weight': weight,
    }, ensure_ascii=False)
