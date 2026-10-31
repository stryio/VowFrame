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
from .face_swap import (FaceSources, FaceSwapError, FaceSwapper, ScoreItem, create_face_swapper,
                        face_swap_applied_snapshot, face_swap_snapshot, face_swap_weight)
from .models import DEFAULT_MODEL
from .prompts import DETAIL_SHOT, POSE_BY_ID, STYLING_ASPECT_RATIO, compose_look_prompt, is_faceless_shot
from .storage import asset_path, save_image
from .styling import find_side_look, task_face_swap_reference_ids, uploaded_asset_id

logger = logging.getLogger(__name__)
# Pictures per face-scoring run.
SCORE_CHUNK = 20


class TaskManager:
    def __init__(self, config: Config, db: Database, generator: ImageGenerator, swapper: FaceSwapper | None = None,
                 stylist: ImageGenerator | None = None):
        self.config = config
        self.db = db
        self.generator = generator
        # Edits uploaded photos into looks (V5.0); the same image model unless a test injects another.
        self.stylist = stylist or generator
        self.swapper = swapper or create_face_swapper(config)
        # One edit per side look: shots of a set run in parallel and wait for the first one to make it.
        self._styling_locks: dict[tuple, asyncio.Lock] = {}
        # Looks waiting for their likeness score against the uploaded photo.
        self._look_score_queue: list[str] = []
        self._wake = asyncio.Event()
        self._stopping = False
        self._jobs: set[asyncio.Task] = set()
        self._face_jobs: set[asyncio.Task] = set()
        # Versions waiting for a face-similarity score; scored in the face lane when no swap is waiting.
        self._score_queue: list[str] = []
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
            # Look candidates: one still being edited may have been charged; queued ones never started.
            conn.execute("UPDATE looks SET status='interrupted',error_code='interrupted_by_restart',error_message=?,finished_at=? "
                         "WHERE status='running'", ('后端重启时这张造型还在生成，结果无法确认，可能已经扣费。重试可能产生额外费用', utc_now()))
            conn.execute("UPDATE looks SET status='failed',error_code='not_started',error_message=?,finished_at=? "
                         "WHERE status='waiting'", ('后端重启时这张造型还在排队，没有开始生成，可以直接重试', utc_now()))
        self.config.temp_dir.mkdir(parents=True, exist_ok=True)
        for path in self.config.temp_dir.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()

    async def start(self):
        self._loop = asyncio.get_running_loop()
        if self.swapper.status['available']:
            # Pictures made before scoring existed, or whose scoring was cut short by a restart.
            with self.db.connect() as conn:
                self._score_queue = [row['id'] for row in conn.execute(
                    'SELECT version.id FROM photo_versions version '
                    'JOIN generation_tasks task ON task.id=version.generation_task_id '
                    'WHERE version.face_scores IS NULL AND (task.moment_shot_size IS NULL OR task.moment_shot_size<>?) '
                    'ORDER BY version.created_at DESC', (DETAIL_SHOT,))]
                self._look_score_queue = [row['id'] for row in conn.execute(
                    "SELECT id FROM looks WHERE angle='front' AND status='ready' AND face_score IS NULL "
                    'AND discarded_at IS NULL ORDER BY created_at DESC')]
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

    def _claim_look(self):
        with self.db.connect(write=True) as conn:
            look = conn.execute("SELECT * FROM looks WHERE status='waiting' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if look is None:
                return None
            changed = conn.execute("UPDATE looks SET status='running' WHERE id=? AND status='waiting'", (look['id'],)).rowcount
            return dict(look) if changed else None

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
                    # Look candidates go first: the user is waiting on them before anything else can be shot.
                    look = self._claim_look()
                    if look is not None:
                        job = asyncio.create_task(self._execute_look(look), name=f"look-{look['id']}")
                        self._jobs.add(job)
                        job.add_done_callback(lambda completed: self._job_done(completed, self._jobs))
                        continue
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
                if not self._face_jobs and self._look_score_queue and not self._stopping:
                    chunk, self._look_score_queue = self._look_score_queue[:SCORE_CHUNK], self._look_score_queue[SCORE_CHUNK:]
                    job = asyncio.create_task(self._execute_look_scoring(chunk), name='look-score')
                    self._face_jobs.add(job)
                    job.add_done_callback(lambda completed: self._job_done(completed, self._face_jobs))
                if not self._face_jobs and self._score_queue and not self._stopping:
                    # One FaceFusion start per chunk; small chunks keep a waiting swap from queueing behind a long backlog.
                    chunk, self._score_queue = self._score_queue[:SCORE_CHUNK], self._score_queue[SCORE_CHUNK:]
                    job = asyncio.create_task(self._execute_scoring(chunk), name='face-score')
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
        snapshot = self._snapshot(task)
        model = snapshot.get('model') or self.db.get_default_model() or DEFAULT_MODEL
        references = json.loads(task['reference_image_ids'])
        if snapshot.get('styling') and not self._styling_done(references, snapshot):
            # A V4.0 task that never got its styled photos: that automatic per-style styling is retired (V5.0).
            self._fail(task, 'styling_retired', '旧版造型流程已停用，请先选好造型，再重新生成样片')
            return
        if snapshot.get('looks'):
            try:
                references = await self._side_looks(task, references, snapshot, model)
                task = {**task, 'reference_image_ids': json.dumps(references)}
            except GeneratorError as error:
                self._handle_generator_error(task, error)
                return
            except FileNotFoundError:
                logger.exception('Looks unavailable for task %s', task['id'])
                self._fail(task, 'reference_missing', '参考照片文件缺失或无法读取，请重新上传后重试')
                return
            except OSError:
                logger.exception('Side look storage failed for task %s', task['id'])
                self._fail(task, 'storage_error', f'图片保存失败，请检查存储空间及目录权限：{self.config.data_dir}', hold=True)
                return
            except Exception:
                logger.exception('Unexpected side look failure for task %s', task['id'])
                self._fail(task, 'generation_failed', '侧面造型照生成失败，请稍后重试')
                return
        try:
            with self.db.connect() as conn:
                images = []
                for asset_id in references:
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

    @staticmethod
    def _snapshot(task: dict) -> dict:
        try:
            snapshot = json.loads(task['model_config_snapshot'])
        except (TypeError, ValueError):
            return {}
        return snapshot if isinstance(snapshot, dict) else {}

    def _styling_done(self, references: list, snapshot: dict) -> bool:
        """Whether a V4.0 task already holds styled photos for all its people's photos."""
        with self.db.connect() as conn:
            for asset_id, slot in zip(references, snapshot.get('reference_slots') or []):
                if slot is None:
                    continue
                row = conn.execute('SELECT asset_type FROM image_assets WHERE id=?', (asset_id,)).fetchone()
                if row is None or row['asset_type'] != 'styled':
                    return False
        return True

    async def _side_looks(self, task: dict, references: list, snapshot: dict, model: str) -> list:
        """Replace each side photo among the references by its side look, editing the ones not made yet.

        The fronts are the task's looks from the start. The task then records the side looks it used, so
        replaying it reuses them.
        """
        looks = snapshot['looks']
        result = list(references)
        for index, (asset_id, slot) in enumerate(zip(references, snapshot.get('reference_slots') or [])):
            if slot is None or slot[1] == 'front':
                continue
            with self.db.connect() as conn:
                asset = conn.execute('SELECT asset_type FROM image_assets WHERE id=?', (asset_id,)).fetchone()
            if asset is None:
                raise FileNotFoundError(f'Reference asset missing: {asset_id}')
            if asset['asset_type'] != 'styled':
                result[index] = await self._side_look(task, looks[slot[0]], slot[1], asset_id, model)
        if result != references:
            with self.db.connect(write=True) as conn:
                conn.execute('UPDATE generation_tasks SET reference_image_ids=? WHERE id=?', (json.dumps(result), task['id']))
        return result

    async def _side_look(self, task: dict, parent_id: str, angle: str, source_id: str, model: str) -> str:
        """The side look made from this side photo and front look; edited now when there is none yet."""
        key = (parent_id, angle, source_id)
        lock = self._styling_locks.setdefault(key, asyncio.Lock())
        async with lock:
            with self.db.connect() as conn:
                existing = find_side_look(conn, parent_id, angle, source_id)
                if existing and self._asset_file(conn, existing) is not None:
                    return existing
                parent = conn.execute('SELECT * FROM looks WHERE id=?', (parent_id,)).fetchone()
                if parent is None or parent['image_asset_id'] is None:
                    raise FileNotFoundError(f'Look missing for task {task["id"]}')
                paths = [self._asset_file(conn, source_id), self._asset_file(conn, parent['image_asset_id'])]
            if existing:
                # The side look's file is gone (e.g. the data folder was cleaned up): retire it and edit again.
                logger.warning('Side look file missing, styling again task_id=%s asset_id=%s', task['id'], existing)
                with self.db.connect(write=True) as conn:
                    conn.execute("UPDATE looks SET discarded_at=? WHERE image_asset_id=? AND discarded_at IS NULL",
                                 (utc_now(), existing))
            if None in paths:
                raise FileNotFoundError(f'Photo to style missing for task {task["id"]}')
            prompt = compose_look_prompt(parent['role'], parent['hairstyle_id'], parent['makeup_level'],
                                         parent['makeup_color'], facing=angle)
            started = time.monotonic()
            logger.info('Side look request started model=%s task_id=%s role=%s angle=%s', model, task['id'], parent['role'], angle)
            content = await asyncio.to_thread(self.stylist.generate_image, paths, prompt, model, STYLING_ASPECT_RATIO)
            logger.info('Side look request succeeded model=%s task_id=%s elapsed_seconds=%.3f',
                        model, task['id'], time.monotonic() - started)
            asset_id = new_id()
            relative_path = await asyncio.to_thread(save_image, self.config, task['project_id'], asset_id, content)
            now = utc_now()
            with self.db.connect(write=True) as conn:
                conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at,source_asset_id) '
                             "VALUES (?,?,?,'styled',?,?)", (asset_id, task['project_id'], relative_path, now, source_id))
                conn.execute(
                    'INSERT INTO looks (id,project_id,person,role,angle,parent_look_id,hairstyle_id,hair_length,makeup_color,'
                    'makeup_level,source_asset_id,styling_version,status,model,prompt,image_asset_id,created_at,finished_at) '
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'ready',?,?,?,?,?)",
                    (new_id(), task['project_id'], parent['person'], parent['role'], angle, parent_id, parent['hairstyle_id'],
                     parent['hair_length'], parent['makeup_color'], parent['makeup_level'], source_id, parent['styling_version'],
                     model, prompt, asset_id, now, now))
            return asset_id

    async def _execute_look(self, look: dict):
        """Edit an uploaded photo into a look candidate the user asked for."""
        model = look['model'] or self.db.get_default_model() or DEFAULT_MODEL
        try:
            with self.db.connect() as conn:
                path = self._asset_file(conn, look['source_asset_id'])
            if path is None:
                self._fail_look(look, 'reference_missing', '这个人的照片文件缺失或无法读取，请重新上传后重试')
                return
            started = time.monotonic()
            logger.info('Look request started model=%s look_id=%s', model, look['id'])
            content = await asyncio.to_thread(self.stylist.generate_image, [path], look['prompt'], model, STYLING_ASPECT_RATIO)
            logger.info('Look request succeeded model=%s look_id=%s elapsed_seconds=%.3f', model, look['id'], time.monotonic() - started)
        except GeneratorError as error:
            logger.warning('Generator outcome %s for look %s', error.kind, look['id'])
            if error.kind == OutcomeKind.RATE_LIMITED:
                self._rate_limit_count += 1
                delay = error.retry_after
                if delay is None or not math.isfinite(delay) or delay < 0:
                    delay = min(60.0, 2.0 ** min(self._rate_limit_count, 6))
                self._backoff_until = max(self._backoff_until, time.monotonic() + max(0.05, delay))
                with self.db.connect(write=True) as conn:
                    conn.execute("UPDATE looks SET status='waiting' WHERE id=? AND status='running'", (look['id'],))
                return
            self._fail_look(look, error.error_code, error.message,
                            status='interrupted' if error.kind == OutcomeKind.UNKNOWN_OUTCOME else 'failed')
            return
        except Exception:
            logger.exception('Unexpected look failure for look %s', look['id'])
            self._fail_look(look, 'generation_failed', '造型生成失败，请稍后重试')
            return
        try:
            asset_id = new_id()
            relative_path = await asyncio.to_thread(save_image, self.config, look['project_id'], asset_id, content)
            now = utc_now()
            with self.db.connect(write=True) as conn:
                conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at,source_asset_id) '
                             "VALUES (?,?,?,'styled',?,?)", (asset_id, look['project_id'], relative_path, now, look['source_asset_id']))
                conn.execute("UPDATE looks SET status='ready',image_asset_id=?,model=?,error_code=NULL,error_message=NULL,"
                             "finished_at=? WHERE id=? AND status='running'", (asset_id, model, now, look['id']))
                conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, look['project_id']))
            self._rate_limit_count = 0
            if self.swapper.status['available']:
                self._look_score_queue.append(look['id'])
                self._wake.set()
        except OSError:
            logger.exception('Look storage failed for look %s', look['id'])
            self._fail_look(look, 'storage_error', f'图片保存失败，请检查存储空间及目录权限：{self.config.data_dir}')
        except Exception:
            logger.exception('Look persistence failed for look %s', look['id'])
            self._fail_look(look, 'save_failed', '造型保存失败，请检查本地数据后重试')

    def _fail_look(self, look: dict, code: str, message: str, *, status: str = 'failed'):
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE looks SET status=?,error_code=?,error_message=?,finished_at=? WHERE id=? AND status='running'",
                         (status, code, message, utc_now(), look['id']))
            conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), look['project_id']))

    async def _execute_look_scoring(self, look_ids: list[str]):
        """Likeness of each look to the uploaded photo it was made from, shown on the candidate card."""
        try:
            items, scored = [], []
            with self.db.connect() as conn:
                for look_id in look_ids:
                    look = conn.execute('SELECT role,source_asset_id,image_asset_id FROM looks WHERE id=?', (look_id,)).fetchone()
                    if look is None or look['image_asset_id'] is None:
                        continue
                    source, target = self._asset_file(conn, look['source_asset_id']), self._asset_file(conn, look['image_asset_id'])
                    if source is not None and target is not None:
                        # One person per picture: the other role finds no face of its gender and stays empty.
                        items.append(ScoreItem(source, source, target))
                        scored.append((look_id, look['role']))
            if not items:
                return
            scores = await asyncio.to_thread(self.swapper.score, items)
            with self.db.connect(write=True) as conn:
                for (look_id, role), value in zip(scored, scores):
                    if value is not None and value.get(role) is not None:
                        conn.execute('UPDATE looks SET face_score=? WHERE id=? AND face_score IS NULL', (value[role], look_id))
        except Exception:
            logger.warning('Look scoring failed for %s looks', len(look_ids), exc_info=True)

    def _asset_file(self, conn, asset_id: str):
        """Path of an image asset's file; None when the asset or its file is missing."""
        row = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (asset_id,)).fetchone()
        path = asset_path(self.config, row['file_path']) if row else None
        return path if path is not None and path.is_file() else None

    async def _execute_face_swap(self, task: dict):
        try:
            with self.db.connect() as conn:
                references = json.loads(task['reference_image_ids'])
                if not isinstance(references, list) or len(references) < 2 or not references[0] or not references[1]:
                    raise FileNotFoundError('Face-swap references are incomplete')
                # [bride front, groom front, bride left, bride right, groom left, groom right]; tasks from before
                # side photos carry only the two fronts.
                references = references + [None] * (6 - len(references))
                paths = []
                for asset_id in references[:6]:
                    if asset_id is None:
                        paths.append(None)
                        continue
                    row = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (asset_id,)).fetchone()
                    if row is None:
                        raise FileNotFoundError(f'Reference asset missing: {asset_id}')
                    path = asset_path(self.config, row['file_path'])
                    if not path.is_file():
                        raise FileNotFoundError(path)
                    paths.append(path)
                bride = FaceSources(paths[0], paths[2], paths[3])
                groom = FaceSources(paths[1], paths[4], paths[5])
                # The swap measures likeness against its own sources; with styled sources that is not the uploaded
                # photos every other score compares with, so such pictures are scored separately instead.
                styled_sources = any(uploaded_asset_id(conn, asset_id) != asset_id for asset_id in references[:2])
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
            weight = json.loads(task['model_config_snapshot'])['weight']
            result = await asyncio.to_thread(self.swapper.swap, bride, groom, target_path, weight)
            content = result.content
            if not content:
                raise FaceSwapError('face_swap_failed')
            # Which model each face got is only known after measuring it; keep it on the task.
            with self.db.connect(write=True) as conn:
                conn.execute('UPDATE generation_tasks SET model_config_snapshot=? WHERE id=?',
                             (face_swap_applied_snapshot(task['model_config_snapshot'], result.faces), task['id']))
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
            self._commit_result(task, asset_id, relative_path, None if styled_sources else result.scores,
                                score_later=styled_sources)
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
        if task['moment_shot_size'] and is_faceless_shot(task['prompt']):
            return None  # back views and silhouettes show no faces; a manual swap is still available
        if not self.db.get_auto_face_swap():
            return None
        status = self.swapper.status
        return status['mode'] if status['available'] else None

    async def _execute_scoring(self, version_ids: list[str]):
        try:
            items, scored = [], []
            with self.db.connect() as conn:
                for version_id in version_ids:
                    row = conn.execute(
                        'SELECT asset.file_path,task.reference_image_ids FROM photo_versions version '
                        'JOIN image_assets asset ON asset.id=version.image_asset_id '
                        'JOIN generation_tasks task ON task.id=version.generation_task_id WHERE version.id=?',
                        (version_id,),
                    ).fetchone()
                    if row is None:
                        continue  # deleted while waiting
                    # Every task keeps [bride front, groom front] first.
                    # Similarity is measured against the uploaded photos, not the styled ones (V4.0).
                    references = [uploaded_asset_id(conn, asset_id) for asset_id in json.loads(row['reference_image_ids'])[:2]]
                    paths = [conn.execute('SELECT file_path FROM image_assets WHERE id=?', (asset_id,)).fetchone()
                             for asset_id in references]
                    if len(paths) < 2 or None in paths:
                        continue
                    bride, groom, target = (asset_path(self.config, item['file_path']) for item in (*paths, row))
                    if bride.is_file() and groom.is_file() and target.is_file():
                        items.append(ScoreItem(bride, groom, target))
                        scored.append(version_id)
            if not items:
                return
            scores = await asyncio.to_thread(self.swapper.score, items)
            with self.db.connect(write=True) as conn:
                for version_id, value in zip(scored, scores):
                    if value is not None:
                        conn.execute('UPDATE photo_versions SET face_scores=? WHERE id=? AND face_scores IS NULL',
                                     (json.dumps(value), version_id))
        except Exception:
            # A score is only shown to help judge the picture; never retried in a loop, the next start tries again.
            logger.warning('Face scoring failed for %s versions', len(version_ids), exc_info=True)

    def _commit_result(self, task: dict, asset_id: str, relative_path: str, scores: dict | None = None,
                       score_later: bool = False):
        now = utc_now()
        version_id = new_id()
        swap_mode = self._auto_face_swap_mode(task)
        with self.db.connect(write=True) as conn:
            slot_kind = conn.execute('SELECT kind FROM photo_slots WHERE id=?', (task['target_slot_id'],)).fetchone()['kind']
            # Like a manual swap, an automatic one uses the project's current photos, side photos included.
            project = conn.execute('SELECT * FROM projects WHERE id=?', (task['project_id'],)).fetchone()
            current = conn.execute('SELECT status FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
            if current is None or current['status'] != 'running':
                raise RuntimeError('Cannot save result for a task that is not running')
            number = conn.execute(
                'SELECT COALESCE(MAX(version_number),0)+1 FROM photo_versions WHERE photo_slot_id=?',
                (task['target_slot_id'],),
            ).fetchone()[0]
            asset_type = 'preview' if task['pose_id'] is None and task['moment_title'] is None else 'photo'
            conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at) VALUES (?,?,?,?,?)',
                         (asset_id, task['project_id'], relative_path, asset_type, now))
            scores = scores or {}
            conn.execute('INSERT INTO photo_versions (id,photo_slot_id,image_asset_id,generation_task_id,version_number,'
                         'created_at,face_scores) VALUES (?,?,?,?,?,?,?)',
                         (version_id, task['target_slot_id'], asset_id, task['id'], number, now,
                          json.dumps(scores['after']) if scores.get('after') else None))
            if scores.get('before'):
                # The swap measured the picture it started from too; keep a score made earlier.
                conn.execute('UPDATE photo_versions SET face_scores=? WHERE id=? AND face_scores IS NULL',
                             (json.dumps(scores['before']), task['source_version_id']))
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
                     json.dumps(task_face_swap_reference_ids(conn, project, self._snapshot(task),
                                                            json.loads(task['reference_image_ids']))),
                     task['style_id'], task['pose_id'],
                     task['moment_title'], task['moment_shot_size'], face_swap_snapshot(swap_mode, face_swap_weight(slot_kind)), version_id, now),
                )
            conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, task['project_id']))
        if (swap_mode is None and task['regenerate_mode'] != 'face_swap' and task['moment_shot_size'] != DETAIL_SHOT
                and self.swapper.status['available']):
            # A queued swap measures this picture itself; otherwise score it on its own.
            self._score_queue.append(version_id)
            self._wake.set()
        elif score_later and self.swapper.status['available']:
            # A swap from styled photos: score the swapped picture and the one it started from on their own.
            self._score_queue.extend([version_id, task['source_version_id']])
            self._wake.set()

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
