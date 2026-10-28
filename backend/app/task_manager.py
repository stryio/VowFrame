"""Database-driven scheduling with explicit recovery and durable version commits."""

import asyncio
import json
import logging
import math
import shutil
import time

from .config import Config
from .db import Database, new_id, utc_now
from .generator import GeneratorError, ImageGenerator, OutcomeKind
from .face_swap import FaceSwapError, FaceSwapper, create_face_swapper, face_swap_snapshot
from .models import DEFAULT_MODEL
from .prompts import DETAIL_SHOT, POSE_BY_ID
from .storage import asset_path, save_image

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, config: Config, db: Database, generator: ImageGenerator, swapper: FaceSwapper | None = None):
        self.config = config
        self.db = db
        self.generator = generator
        self.swapper = swapper or create_face_swapper(config)
        self._wake = asyncio.Event()
        self._stopping = False
        self._jobs: set[asyncio.Task] = set()
        self._face_jobs: set[asyncio.Task] = set()
        self._dispatcher: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._backoff_until = 0.0
        self._rate_limit_count = 0

    def recover(self):
        with self.db.connect(write=True) as conn:
            conn.execute(
                "UPDATE generation_tasks SET status='waiting',error_code=NULL,error_message=NULL,started_at=NULL,finished_at=NULL "
                "WHERE status='running' AND regenerate_mode='face_swap'",
            )
            conn.execute(
                "UPDATE generation_tasks SET status='interrupted', error_code='interrupted_by_restart', "
                "error_message=?, finished_at=? WHERE status='running' AND (regenerate_mode IS NULL OR regenerate_mode<>'face_swap')",
                ('后端重启时这次请求还在进行，结果无法确认，可能已经扣费。重新生成可能产生额外费用', utc_now()),
            )
            conn.execute("UPDATE generation_tasks SET status='held' WHERE status='waiting' AND (regenerate_mode IS NULL OR regenerate_mode<>'face_swap')")
        self.config.temp_dir.mkdir(parents=True, exist_ok=True)
        for path in self.config.temp_dir.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self._dispatcher = asyncio.create_task(self._dispatch(), name='vowframe-scheduler')

    def notify(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def stop(self):
        self._stopping = True
        self._wake.set()
        if self._dispatcher:
            await self._dispatcher
        # Submitted requests are allowed to finish; never cancel a provider thread.
        if self._jobs or self._face_jobs:
            await asyncio.gather(*self._jobs, *self._face_jobs)

    def _claim(self, lane: str = 'generation'):
        with self.db.connect(write=True) as conn:
            if lane == 'face_swap':
                condition = "regenerate_mode='face_swap'"
            else:
                condition = "(regenerate_mode IS NULL OR regenerate_mode<>'face_swap')"
            task = conn.execute(
                f"SELECT * FROM generation_tasks WHERE status='waiting' AND {condition} ORDER BY created_at,rowid LIMIT 1"
            ).fetchone()
            if task is None:
                return None
            started = utc_now()
            changed = conn.execute(
                "UPDATE generation_tasks SET status='running', started_at=?, finished_at=NULL "
                "WHERE id=? AND status='waiting'", (started, task['id']),
            ).rowcount
            if not changed:
                return None
            return dict(task)

    async def _dispatch(self):
        while not self._stopping:
            self._wake.clear()
            delay = self._backoff_until - time.monotonic()
            try:
                while delay <= 0 and len(self._jobs) < self.config.max_concurrency and not self._stopping:
                    task = self._claim('generation')
                    if task is None:
                        break
                    job = asyncio.create_task(self._execute(task), name=f"generate-{task['id']}")
                    self._jobs.add(job)
                    job.add_done_callback(lambda completed: self._job_done(completed, self._jobs))
                while len(self._face_jobs) < 1 and not self._stopping:
                    task = self._claim('face_swap')
                    if task is None:
                        break
                    job = asyncio.create_task(self._execute_face_swap(task), name=f"face-swap-{task['id']}")
                    self._face_jobs.add(job)
                    job.add_done_callback(lambda completed: self._job_done(completed, self._face_jobs))
            except Exception:
                logger.exception('Task dispatch failed')
            # Notifications drive normal scheduling; polling also finds externally persisted work.
            timeout = min(max(delay, 0.01), 1.0) if delay > 0 else 1.0
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass

    def _job_done(self, job: asyncio.Task, jobs: set[asyncio.Task]):
        jobs.discard(job)
        if not job.cancelled() and job.exception() is not None:
            logger.error('Task persistence failed', exc_info=job.exception())
        self._wake.set()

    async def _execute(self, task: dict):
        try:
            with self.db.connect() as conn:
                images = []
                for asset_id in json.loads(task['reference_image_ids']):
                    row = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (asset_id,)).fetchone()
                    if row is None:
                        raise FileNotFoundError(f'Reference asset missing: {asset_id}')
                    path = asset_path(self.config, row['file_path'])
                    if not path.is_file():
                        raise FileNotFoundError(path)
                    images.append(path)
        except Exception:
            logger.exception('Reference images unavailable for task %s', task['id'])
            self._fail(task, 'reference_missing', '参考照片文件缺失或无法读取，请重新上传后重试')
            return

        try:
            try:
                snapshot = json.loads(task['model_config_snapshot'])
            except (TypeError, ValueError):
                snapshot = {}
            if not isinstance(snapshot, dict):
                snapshot = {}
            model = snapshot.get('model') or self.db.get_default_model() or DEFAULT_MODEL
            aspect_ratio = snapshot.get('aspect_ratio') or self.config.aspect_ratio
            started = time.monotonic()
            logger.info('Generation request started model=%s task_id=%s', model, task['id'])
            content = await asyncio.to_thread(self.generator.generate_image, images, task['prompt'], model, aspect_ratio)
            logger.info('Generation request succeeded model=%s task_id=%s elapsed_seconds=%.3f',
                        model, task['id'], time.monotonic() - started)
        except GeneratorError as error:
            self._handle_generator_error(task, error)
            return
        except Exception:
            logger.exception('Unexpected generator failure for task %s', task['id'])
            self._fail(task, 'generation_failed', '图片生成失败，请稍后重试')
            return

        try:
            asset_id = new_id()
            relative_path = await asyncio.to_thread(save_image, self.config, task['project_id'], asset_id, content)
            self._commit_result(task, asset_id, relative_path)
            self._rate_limit_count = 0
        except OSError:
            logger.exception('Image storage failed for task %s', task['id'])
            self._fail(task, 'storage_error', f'图片保存失败，请检查存储空间及目录权限：{self.config.data_dir}', hold=True)
        except Exception:
            logger.exception('Result persistence failed for task %s', task['id'])
            self._fail(task, 'save_failed', '生成结果保存失败，请检查本地数据后重试', hold=True)

    async def _execute_face_swap(self, task: dict):
        try:
            with self.db.connect() as conn:
                references = json.loads(task['reference_image_ids'])
                if not isinstance(references, list) or len(references) < 2:
                    raise FileNotFoundError('Face-swap references are incomplete')
                paths = []
                for asset_id in references[:2]:
                    row = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (asset_id,)).fetchone()
                    if row is None:
                        raise FileNotFoundError(f'Reference asset missing: {asset_id}')
                    path = asset_path(self.config, row['file_path'])
                    if not path.is_file():
                        raise FileNotFoundError(path)
                    paths.append(path)
                source_version = conn.execute(
                    'SELECT image_asset_id FROM photo_versions WHERE id=? AND photo_slot_id=?',
                    (task['source_version_id'], task['target_slot_id']),
                ).fetchone()
                if source_version is None:
                    raise FileNotFoundError('Face-swap source version is missing')
                target_asset = conn.execute(
                    'SELECT file_path FROM image_assets WHERE id=?', (source_version['image_asset_id'],)
                ).fetchone()
                target_path = asset_path(self.config, target_asset['file_path']) if target_asset else None
                if target_path is None or not target_path.is_file():
                    raise FileNotFoundError('Face-swap target image is missing')
        except Exception:
            logger.warning('Face-swap inputs unavailable for task %s', task['id'])
            self._fail(task, 'reference_missing', '参考照片文件缺失或无法读取，请重新上传后重试')
            return

        try:
            started = time.monotonic()
            content = await asyncio.to_thread(self.swapper.swap, paths[0], paths[1], target_path)
            if not content:
                raise FaceSwapError('face_swap_failed')
            logger.info('Face-swap succeeded task_id=%s elapsed_seconds=%.3f', task['id'], time.monotonic() - started)
        except FaceSwapError as exc:
            self._handle_face_swap_error(task, exc)
            return
        except Exception:
            logger.exception('Unexpected face-swap failure for task %s', task['id'])
            self._handle_face_swap_error(task, FaceSwapError('face_swap_failed'))
            return
        try:
            asset_id = new_id()
            relative_path = await asyncio.to_thread(save_image, self.config, task['project_id'], asset_id, content)
            self._commit_result(task, asset_id, relative_path)
        except OSError:
            self._fail(task, 'storage_error', '图片保存失败，请检查存储空间及目录权限', hold=False)
        except Exception:
            logger.exception('Face-swap result persistence failed for task %s', task['id'])
            self._fail(task, 'save_failed', '换脸结果保存失败，请稍后重试', hold=False)

    def _handle_face_swap_error(self, task: dict, error: FaceSwapError):
        logger.warning('Face-swap task %s failed with %s', task['id'], error.error_code)
        if error.error_code == 'face_swap_failed' and task['auto_retry_count'] < 2:
            with self.db.connect(write=True) as conn:
                conn.execute(
                    "UPDATE generation_tasks SET status='waiting',auto_retry_count=auto_retry_count+1,error_code=?,error_message=?,started_at=NULL,finished_at=NULL WHERE id=? AND status='running'",
                    (error.error_code, error.message, task['id']),
                )
            return
        self._fail(task, error.error_code, error.message, hold=False)

    def _auto_face_swap_mode(self, task: dict) -> str | None:
        """Swapper mode to queue after this task, or None when no automatic swap applies."""
        if task['regenerate_mode'] == 'face_swap' or task['moment_shot_size'] == DETAIL_SHOT:
            return None  # never swap a swap; detail shots have no faces to swap
        if not self.db.get_auto_face_swap():
            return None
        status = self.swapper.status
        return status['mode'] if status['available'] else None

    def _commit_result(self, task: dict, asset_id: str, relative_path: str):
        now = utc_now()
        version_id = new_id()
        swap_mode = self._auto_face_swap_mode(task)
        with self.db.connect(write=True) as conn:
            current = conn.execute('SELECT status FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
            if current is None or current['status'] != 'running':
                raise RuntimeError('Cannot save result for a task that is not running')
            number = conn.execute(
                'SELECT COALESCE(MAX(version_number),0)+1 FROM photo_versions WHERE photo_slot_id=?',
                (task['target_slot_id'],),
            ).fetchone()[0]
            asset_type = 'preview' if task['pose_id'] is None and task['moment_title'] is None else 'photo'
            conn.execute('INSERT INTO image_assets VALUES (?,?,?,?,?)',
                         (asset_id, task['project_id'], relative_path, asset_type, now))
            conn.execute('INSERT INTO photo_versions VALUES (?,?,?,?,?,?)',
                         (version_id, task['target_slot_id'], asset_id, task['id'], number, now))
            pose_name = task['moment_title'] or (POSE_BY_ID.get(task['pose_id'], {}).get('name') if task['pose_id'] else None)
            conn.execute(
                'UPDATE photo_slots SET current_version_id=?,pose_id=?,pose_name=?,shot_size=? WHERE id=?',
                (version_id, task['pose_id'], pose_name, task['moment_shot_size'], task['target_slot_id']),
            )
            conn.execute(
                "UPDATE generation_tasks SET status='completed', output_asset_id=?, finished_at=?, "
                'error_code=NULL,error_message=NULL WHERE id=?', (asset_id, now, task['id']),
            )
            if swap_mode is not None:
                # Same transaction as the new version: the swap is queued exactly when the original exists.
                # It joins the task's batch, so the batch finishes only after the swapped image is ready.
                conn.execute(
                    'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,'
                    'style_id,pose_id,moment_title,moment_shot_size,prompt,model_config_snapshot,regenerate_mode,'
                    "source_version_id,created_at) VALUES (?,?,?,?,'waiting',?,?,?,?,?,'',?,'face_swap',?,?)",
                    (new_id(), task['project_id'], task['batch_id'], task['target_slot_id'],
                     json.dumps(json.loads(task['reference_image_ids'])[:2]), task['style_id'], task['pose_id'],
                     task['moment_title'], task['moment_shot_size'], face_swap_snapshot(swap_mode), version_id, now),
                )
            conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, task['project_id']))

    def _fail(self, task: dict, code: str, message: str, *, hold: bool = False, status: str = 'failed'):
        with self.db.connect(write=True) as conn:
            conn.execute(
                'UPDATE generation_tasks SET status=?,error_code=?,error_message=?,finished_at=? '
                "WHERE id=? AND status='running'", (status, code, message, utc_now(), task['id']),
            )
            if hold:
                conn.execute("UPDATE generation_tasks SET status='held' WHERE batch_id=? AND status='waiting' AND (regenerate_mode IS NULL OR regenerate_mode<>'face_swap')",
                             (task['batch_id'],))
            conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), task['project_id']))

    def _handle_generator_error(self, task: dict, error: GeneratorError):
        logger.warning('Generator outcome %s for task %s', error.kind, task['id'])
        if error.kind == OutcomeKind.NOT_SENT and task['auto_retry_count'] < 2:
            with self.db.connect(write=True) as conn:
                conn.execute(
                    "UPDATE generation_tasks SET status='waiting',auto_retry_count=auto_retry_count+1, "
                    "error_code=?,error_message=?,started_at=NULL WHERE id=? AND status='running'",
                    (error.error_code, error.message, task['id']),
                )
            return
        if error.kind == OutcomeKind.RATE_LIMITED:
            self._rate_limit_count += 1
            delay = error.retry_after
            if delay is None or not math.isfinite(delay) or delay < 0:
                delay = min(60.0, 2.0 ** min(self._rate_limit_count, 6))
            self._backoff_until = max(self._backoff_until, time.monotonic() + max(0.05, delay))
            with self.db.connect(write=True) as conn:
                conn.execute(
                    "UPDATE generation_tasks SET status='waiting',error_code=?,error_message=?,started_at=NULL "
                    "WHERE id=? AND status='running'", (error.error_code, error.message, task['id']),
                )
            return
        self._fail(task, error.error_code, error.message,
                   hold=error.kind == OutcomeKind.AUTH_OR_QUOTA,
                   status='interrupted' if error.kind == OutcomeKind.UNKNOWN_OUTCOME else 'failed')
