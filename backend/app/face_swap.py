"""Local face-swap provider boundary and isolated FaceFusion process adapter."""

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
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


# Chosen by the user on 2026-10-28: a face in the generated image that looks at the camera is swapped with
# 1C from the front photo; a turned face is swapped with 1B from the front photo plus the matching side photo
# (FaceFusion averages the identities of several source photos).
FRONT_FACE_MODEL = 'hyperswap_1c_256'
SIDE_FACE_MODEL = 'hyperswap_1b_256'
# Face scoring loads FaceFusion once per call, then reads each picture.
SCORE_TIMEOUT_SECONDS = 120
SCORE_SECONDS_PER_IMAGE = 5
# How far the nose sits from the eye midpoint, along the eye line, in eye distances. Calibrated on 146 faces
# of real generated photos on 2026-10-28: ~0.12–0.16 is a slight turn (~15–20°), from ~0.19 a clear side
# view; 0.18 sits between a frontal face and the ~45° side photos.
SIDE_FACE_TURN = 0.18


@dataclass(frozen=True)
class FaceSources:
    """One person's uploaded photos; side photos are optional. Directions are as seen in the picture."""
    front: Path
    left: Path | None = None
    right: Path | None = None


@dataclass(frozen=True)
class FaceSwapResult:
    content: bytes
    # Per role ('bride' / 'groom'): turn, facing, model and the number of source photos actually used.
    faces: dict = field(default_factory=dict)
    # Face similarity of the picture before and after the swap, each as face_scores() returns it.
    scores: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreItem:
    """A finished picture and the two front photos it is compared with."""
    bride: Path
    groom: Path
    target: Path


def face_scores(value) -> dict | None:
    """{'bride': x, 'groom': y}: cosine similarity of FaceFusion's ArcFace embeddings between the person's front
    photo and their face in the picture; None for a person whose face was not found. None when not measured."""
    if not isinstance(value, dict):
        return None
    result = {}
    for role in ('bride', 'groom'):
        score = value.get(role)
        result[role] = round(float(score), 3) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    return result


def face_swap_reference_ids(project) -> list[str | None]:
    """Face-swap task references: [bride front, groom front, bride left, bride right, groom left, groom right].

    The fronts stay first like every other task; a missing side photo is null so each slot keeps its meaning.
    """
    a = (project['person_a_image_id'], project['person_a_left_image_id'], project['person_a_right_image_id'])
    b = (project['person_b_image_id'], project['person_b_left_image_id'], project['person_b_right_image_id'])
    bride, groom = (a, b) if project['person_a_role'] == 'bride' else (b, a)
    return [bride[0], groom[0], bride[1], bride[2], groom[1], groom[2]]


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

    def swap(self, bride: FaceSources, groom: FaceSources, target: Path, weight: float) -> FaceSwapResult: ...

    def score(self, items: list[ScoreItem]) -> list[dict | None]: ...


class MockFaceSwapper:
    @property
    def status(self) -> dict:
        return {'available': True, 'mode': 'mock', 'reason': None}

    def swap(self, bride: FaceSources, groom: FaceSources, target: Path, weight: float) -> FaceSwapResult:
        return FaceSwapResult(target.read_bytes())

    def score(self, items: list[ScoreItem]) -> list[dict | None]:
        return [None] * len(items)


class UnavailableFaceSwapper:
    def __init__(self, reason: str):
        self.reason = reason

    @property
    def status(self) -> dict:
        return {'available': False, 'mode': None, 'reason': self.reason}

    def swap(self, bride: FaceSources, groom: FaceSources, target: Path, weight: float) -> FaceSwapResult:
        raise FaceSwapError('face_swap_failed')

    def score(self, items: list[ScoreItem]) -> list[dict | None]:
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

    def swap(self, bride: FaceSources, groom: FaceSources, target: Path, weight: float) -> FaceSwapResult:
        if not self.status['available']:
            raise FaceSwapError('face_swap_failed')

        def sources(person: FaceSources) -> dict:
            return {angle: str(Path(path).resolve()) if path else None
                    for angle, path in (('front', person.front), ('left', person.left), ('right', person.right))}

        with tempfile.TemporaryDirectory(prefix='vowframe-face-swap-') as directory:
            output = Path(directory) / 'result.png'
            payload = {
                'facefusion_dir': str(self.facefusion_dir),
                'bride': sources(bride),
                'groom': sources(groom),
                'target_path': str(Path(target).resolve()),
                'output_path': str(output),
                'work_dir': directory,
                'weight': weight,
                'front_model': FRONT_FACE_MODEL,
                'side_model': SIDE_FACE_MODEL,
                'side_turn': SIDE_FACE_TURN,
            }
            completed, result = self._call_helper(payload, self.timeout_seconds)
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
            faces = result.get('faces')
            if not isinstance(faces, dict):
                logger.warning('FaceFusion helper did not report the per-face models')
                raise FaceSwapError('face_swap_failed')
            try:
                scores = result.get('scores') if isinstance(result.get('scores'), dict) else {}
                return FaceSwapResult(output.read_bytes(), faces,
                                      {key: face_scores(scores.get(key)) for key in ('before', 'after')})
            except OSError:
                logger.warning('FaceFusion output could not be read')
                raise FaceSwapError('face_swap_failed') from None

    def _call_helper(self, payload: dict, timeout: float) -> tuple[subprocess.CompletedProcess, object]:
        """Run the helper in FaceFusion's own venv; returns the process and its decoded JSON answer."""
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
                timeout=timeout,
                cwd=self.facefusion_dir,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning('FaceFusion helper timed out after %s seconds', timeout)
            raise FaceSwapError('face_swap_failed') from None
        except OSError as exc:
            logger.warning('FaceFusion helper could not start (%s)', type(exc).__name__)
            raise FaceSwapError('face_swap_failed') from None
        try:
            return completed, json.loads(completed.stdout)
        except (TypeError, ValueError):
            logger.warning('FaceFusion helper returned invalid JSON (exit_status=%s)', completed.returncode)
            raise FaceSwapError('face_swap_failed') from None

    def score(self, items: list[ScoreItem]) -> list[dict | None]:
        with tempfile.TemporaryDirectory(prefix='vowframe-face-score-') as directory:
            payload = {
                'action': 'score',
                'facefusion_dir': str(self.facefusion_dir),
                'work_dir': directory,
                'items': [{'bride': str(Path(item.bride).resolve()), 'groom': str(Path(item.groom).resolve()),
                           'target': str(Path(item.target).resolve())} for item in items],
            }
            completed, result = self._call_helper(payload, SCORE_TIMEOUT_SECONDS + SCORE_SECONDS_PER_IMAGE * len(items))
        scores = result.get('scores') if isinstance(result, dict) and result.get('ok') else None
        if completed.returncode != 0 or not isinstance(scores, list) or len(scores) != len(items):
            diagnostic = result.get('diagnostic') if isinstance(result, dict) else None
            logger.warning('FaceFusion face scoring failed (exit_status=%s, diagnostic=%s)',
                           completed.returncode, diagnostic if isinstance(diagnostic, str) else 'unavailable')
            raise FaceSwapError('face_swap_failed')
        return [face_scores(value) for value in scores]


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
    """Model record stored on face-swap tasks; never contains secrets.

    Which model each face gets is only known once the generated face is measured, so `model` starts as the
    rule summary and face_swap_applied_snapshot records what was actually used.
    """
    return json.dumps({
        'generator': mode,
        'model': f'{FRONT_FACE_MODEL}/{SIDE_FACE_MODEL}',
        'front_model': FRONT_FACE_MODEL,
        'side_model': SIDE_FACE_MODEL,
        'side_turn': SIDE_FACE_TURN,
        'pixel_boost': '512x512',
        'face_mask_types': ['box', 'occlusion'],
        'weight': weight,
    }, ensure_ascii=False)


def face_swap_applied_snapshot(snapshot: str, faces: dict) -> str:
    """Add the per-face outcome; `model` then reads bride model / groom model as actually applied."""
    value = json.loads(snapshot)
    if faces:
        value['faces'] = faces
        value['model'] = f"{faces['bride']['model']}/{faces['groom']['model']}"
    return json.dumps(value, ensure_ascii=False)
