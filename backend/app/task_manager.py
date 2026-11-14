"""Database-driven scheduling with explicit recovery and durable version commits."""

import asyncio
import json
import logging
import math
import shutil
import tempfile
import time
from pathlib import Path

from .config import Config
from .db import Database, new_id, utc_now
from .facings import direction
from .face_fix import face_fix_snapshot, fix_prompt, fix_roles, follow_up_swap_roles
from .generator import GeneratorError, ImageGenerator, OutcomeKind
from .face_swap import (FaceSources, FaceSwapError, FaceSwapper, ScoreItem, create_face_swapper,
                        face_swap_applied_snapshot, face_swap_snapshot, FACE_SWAP_WEIGHT)
from .api88 import size_problem
from .models import DEFAULT_MODEL
from .prompts import (DETAIL_SHOT, POSE_BY_ID, STYLE_ART_ASPECT_RATIO, STYLE_ART_COMBINE_ASPECT_RATIO, STYLE_ART_COMBINE_PROMPT, STYLE_ART_GRAY_PROMPT,
                      style_art_version,
                      STYLING_ASPECT_RATIO, compose_look_prompt, is_faceless_shot, style_art_product_prompt,
                      style_art_scene_prompt)
from .storage import asset_path, reference_copy, save_image, save_room_area_image, save_style_art_image
from .rooms import main_image, room_image_version
from .style_art import (art_area, art_files_present, art_pending, current_style_art, failed_style_art, outfit_product_image,
                        project_art_asset, shared_product)
from .styling import (side_by_side, auto_select, auto_select_makeup, find_side_look, side_look_key, makeup_style_of, task_face_swap_reference_ids)
from .retouch import candidate_retouch_settings
from .regional_edit import RegionalEdit, crop_edit_inputs, crop_edit_prompt, paste_crop_result, png_result, regional_edit_prompt

logger = logging.getLogger(__name__)
# Pictures per face-scoring run.
SCORE_CHUNK = 20
# How much less alike (ArcFace cosine similarity) an AI face fix may make a person before the original is kept (V7.5):
# small differences are measurement noise, not a worse face.
FACE_FIX_TOLERANCE = 0.02


class TaskStopped(Exception):
    """The user stopped the task while its request was out (V7.12): its result is not saved."""


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
        # Makeup candidates (V7.8) waiting for theirs.
        self._makeup_score_queue: list[str] = []
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
            conn.execute("UPDATE generation_tasks SET status='held' WHERE status='waiting' AND batch_id IN "
                         '(SELECT id FROM generation_batches WHERE paused_at IS NOT NULL)')
            # Look candidates: one still being edited may have been charged; queued ones never started.
            for table, noun in (('looks', '造型'), ('makeups', '妆容')):
                conn.execute(f"UPDATE {table} SET status='interrupted',error_code='interrupted_by_restart',error_message=?,finished_at=? "
                             "WHERE status='running'", (f'后端重启时这张{noun}还在生成，结果无法确认，可能已经扣费。重试可能产生额外费用', utc_now()))
                conn.execute(f"UPDATE {table} SET status='failed',error_code='not_started',error_message=?,finished_at=? "
                             "WHERE status='waiting'", (f'后端重启时这张{noun}还在排队，没有开始生成，可以直接重试', utc_now()))
            conn.execute("UPDATE style_arts SET status='interrupted',error_code='interrupted_by_restart',error_message=?,finished_at=? "
                         "WHERE status='running'", ('后端重启时这张服饰场景图还在生成，结果无法确认，可能已经扣费', utc_now()))
        self.config.temp_dir.mkdir(parents=True, exist_ok=True)
        for path in self.config.temp_dir.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()

    async def start(self):
        self._loop = asyncio.get_running_loop()
        with self.db.connect(write=True) as conn:
            # Rebuild pending state with the recovered queues; unavailable scoring must not keep clients polling.
            for table in ('photo_versions', 'looks', 'makeups'):
                conn.execute(f'UPDATE {table} SET face_score_pending=0 WHERE face_score_pending=1')
            if self.swapper.status['available']:
                # Pictures made before scoring existed, or whose scoring was cut short by a restart.
                self._score_queue = [row['id'] for row in conn.execute(
                    'SELECT version.id FROM photo_versions version '
                    'JOIN generation_tasks task ON task.id=version.generation_task_id '
                    'WHERE version.face_scores IS NULL AND (task.moment_shot_size IS NULL OR task.moment_shot_size<>?) '
                    'ORDER BY version.created_at DESC', (DETAIL_SHOT,))]
                self._look_score_queue = [row['id'] for row in conn.execute(
                    "SELECT id FROM looks WHERE status='ready' AND face_score IS NULL "
                    'AND discarded_at IS NULL ORDER BY created_at DESC')]
                self._makeup_score_queue = [row['id'] for row in conn.execute(
                    "SELECT id FROM makeups WHERE status='ready' AND face_score IS NULL AND discarded_at IS NULL "
                    'ORDER BY created_at DESC')]
                for table, ids in (('photo_versions', self._score_queue), ('looks', self._look_score_queue),
                                   ('makeups', self._makeup_score_queue)):
                    conn.executemany(f'UPDATE {table} SET face_score_pending=1 WHERE id=?', [(item_id,) for item_id in ids])
        self._dispatcher = asyncio.create_task(self._dispatch(), name='vowframe-scheduler')

    def _queue_face_score(self, conn, table: str, item_id: str):
        """Publish pending scoring atomically with the image becoming ready."""
        if not self.swapper.status['available']:
            return
        queue = {'photo_versions': self._score_queue, 'looks': self._look_score_queue,
                 'makeups': self._makeup_score_queue}[table]
        conn.execute(f'UPDATE {table} SET face_score_pending=1 WHERE id=?', (item_id,))
        queue.append(item_id)
        self._wake.set()

    def _finish_face_scoring(self, table: str, ids: list[str]):
        """A missing face or a failed measurement is finished too; a null score is not a queue state."""
        with self.db.connect(write=True) as conn:
            conn.executemany(f'UPDATE {table} SET face_score_pending=0 WHERE id=?', [(item_id,) for item_id in ids])

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

    def _claim_candidate(self, table: str):
        """The oldest waiting look or makeup candidate (table 'looks' or 'makeups'), now marked running."""
        with self.db.connect(write=True) as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE status='waiting' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            changed = conn.execute(f"UPDATE {table} SET status='running' WHERE id=? AND status='waiting'", (row['id'],)).rowcount
            return dict(row) if changed else None

    def _claim(self, lane: str = 'generation'):
        with self.db.connect(write=True) as conn:
            if lane == 'face_swap':
                condition = "regenerate_mode='face_swap'"
            else:
                condition = "(regenerate_mode IS NULL OR regenerate_mode<>'face_swap')"
            task = conn.execute(
                f"SELECT * FROM generation_tasks WHERE status='waiting' AND {condition} "
                'AND batch_id IN (SELECT id FROM generation_batches WHERE paused_at IS NULL) '
                'ORDER BY created_at,rowid LIMIT 1'
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
                    # Candidates go first: the user is waiting on them before anything else can be shot; makeups
                    # before looks, since a bride's hair candidates are made on her selected makeup (V7.8).
                    makeup = self._claim_candidate('makeups')
                    if makeup is not None:
                        job = asyncio.create_task(self._execute_makeup(makeup), name=f"makeup-{makeup['id']}")
                        self._jobs.add(job)
                        job.add_done_callback(lambda completed: self._job_done(completed, self._jobs))
                        continue
                    look = self._claim_candidate('looks')
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
                if not self._face_jobs and self._makeup_score_queue and not self._stopping:
                    chunk, self._makeup_score_queue = self._makeup_score_queue[:SCORE_CHUNK], self._makeup_score_queue[SCORE_CHUNK:]
                    job = asyncio.create_task(self._execute_makeup_scoring(chunk), name='makeup-score')
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
        if task['regenerate_mode'] in ('face_fix', 'hand_fix', 'wash', 'regional_edit'):
            await self._execute_face_fix(task)
            return
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
                references = await self._attach_style_art(task, references, snapshot, model)
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
                logger.exception('Unexpected side look or style art failure for task %s', task['id'])
                self._fail(task, 'generation_failed', '侧面造型照或服饰场景图生成失败，请稍后重试')
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

        if snapshot.get('paired_looks') and len(images) >= 2:
            # V7.41: the two looks go as one picture, the bride on the left (the prompt was written for it).
            # V7.43: two or more side looks go as one more picture, in reference order (prompts.pair_looks).
            slots = snapshot.get('reference_slots') or []
            sides = [index for index, slot in enumerate(slots) if slot is not None and slot[1] != 'front'] \
                if snapshot.get('paired_sides') else []
            try:
                self.config.temp_dir.mkdir(parents=True, exist_ok=True)
                merged = [await asyncio.to_thread(side_by_side, images[:2], self.config.temp_dir / f"{task['id']}-looks.png")]
                if len(sides) >= 2:
                    merged.append(await asyncio.to_thread(side_by_side, [images[index] for index in sides],
                                                          self.config.temp_dir / f"{task['id']}-sides.png"))
                    sides_set = set(sides)
                    images = [*merged, *(image for index, image in enumerate(images[2:], 2) if index not in sides_set)]
                else:
                    images = [*merged, *images[2:]]
            except OSError:
                logger.exception('Paired looks picture failed for task %s', task['id'])
                self._fail(task, 'reference_missing', '参考照片文件缺失或无法读取，请重新上传后重试')
                return

        # Stopped while its side looks were made (V7.12): never send the paid shot request.
        with self.db.connect() as conn:
            if conn.execute('SELECT status FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()['status'] != 'running':
                logger.info('Task %s stopped before its request', task['id'])
                return
        try:
            aspect_ratio = snapshot.get('aspect_ratio') or self.config.aspect_ratio
            started = time.monotonic()
            logger.info('Generation request started model=%s task_id=%s', model, task['id'])
            try:
                content = await asyncio.to_thread(self.generator.generate_image, images, task['prompt'], model, aspect_ratio)
            finally:
                if snapshot.get('paired_looks'):
                    (self.config.temp_dir / f"{task['id']}-looks.png").unlink(missing_ok=True)
                    (self.config.temp_dir / f"{task['id']}-sides.png").unlink(missing_ok=True)
            size_warning = self._size_warning(content, model, aspect_ratio, f"task_id={task['id']}")
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
            self._commit_result(task, asset_id, relative_path, size_warning=size_warning)
            self._rate_limit_count = 0
        except TaskStopped:
            logger.info('Result discarded for stopped task %s', task['id'])
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
                # 2026-11-13: a side look is a look: the look step's model, not the shot's.
                result[index] = await self._side_look(task, looks[slot[0]], slot[1], asset_id,
                                                      self.db.get_edit_choice('look')[1])
        if result != references:
            with self.db.connect(write=True) as conn:
                conn.execute('UPDATE generation_tasks SET reference_image_ids=? WHERE id=?', (json.dumps(result), task['id']))
        return result

    async def _side_look(self, task: dict, parent_id: str, facing: str, source_id: str, model: str) -> str:
        """The side look made from this side photo and front look; edited now when there is none yet.
        facing: the slot's side facing (V7.48: a full side too); the row keeps its direction."""
        angle = direction(facing)
        with self.db.connect() as conn:
            key = side_look_key(conn, parent_id, angle, source_id)  # shared by every look row on one image (V7.31)
        lock = self._styling_locks.setdefault(key, asyncio.Lock())
        async with lock:
            with self.db.connect() as conn:
                existing = find_side_look(conn, parent_id, angle, source_id)
                if existing and self._asset_file(conn, existing) is not None:
                    return existing
                parent = conn.execute('SELECT * FROM looks WHERE id=?', (parent_id,)).fetchone()
                if parent is None or parent['image_asset_id'] is None:
                    raise FileNotFoundError(f'Look missing for task {task["id"]}')
                # V7.46: the side photo alone; the front look's styling comes from the same words, not its picture.
                paths = [self._asset_file(conn, source_id)]
            if existing:
                # The side look's file is gone (e.g. the data folder was cleaned up): retire it and edit again.
                logger.warning('Side look file missing, styling again task_id=%s asset_id=%s', task['id'], existing)
                with self.db.connect(write=True) as conn:
                    conn.execute("UPDATE looks SET discarded_at=? WHERE image_asset_id=? AND discarded_at IS NULL",
                                 (utc_now(), existing))
            if None in paths:
                raise FileNotFoundError(f'Photo to style missing for task {task["id"]}')
            prompt = compose_look_prompt(parent['role'], parent['hairstyle_id'], parent['makeup_level'],
                                         style_id=makeup_style_of(parent), facing=facing, jewelry_id=parent['jewelry_id'],
                                         retouch_settings=candidate_retouch_settings(parent) if parent['retouch_settings'] is not None else None,
                                         model_id=model)
            content = await self._edit_side_look(paths, prompt, model, f"task_id={task['id']} angle={angle}")
            asset_id, side_id = new_id(), new_id()
            relative_path = await asyncio.to_thread(save_image, self.config, task['project_id'], asset_id, content)
            now = utc_now()
            with self.db.connect(write=True) as conn:
                conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at,source_asset_id) '
                             "VALUES (?,?,?,'styled',?,?)", (asset_id, task['project_id'], relative_path, now, source_id))
                conn.execute(
                    'INSERT INTO looks (id,project_id,person,role,style_id,angle,parent_look_id,hairstyle_id,hair_length,makeup_color,'
                    'makeup_level,source_asset_id,styling_version,status,model,prompt,image_asset_id,created_at,finished_at,makeup_style_id,retouch_settings) '
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ready',?,?,?,?,?,?,?)",
                    (side_id, task['project_id'], parent['person'], parent['role'], parent['style_id'], angle, parent_id, parent['hairstyle_id'],
                     parent['hair_length'], parent['makeup_color'], parent['makeup_level'], source_id, parent['styling_version'],
                     model, prompt, asset_id, now, now, parent['makeup_style_id'], parent['retouch_settings']))
                self._queue_face_score(conn, 'looks', side_id)
            return asset_id

    async def _attach_style_art(self, task: dict, references: list, snapshot: dict, model: str) -> list:
        """Add the style art to a task made before it was ready (V7.33), making the art now if no task did yet.
        The task then records it, so replaying the task reuses it."""
        if not art_pending(references, snapshot):
            return references
        wanted = snapshot['style_art']
        art = await self.ensure_style_art(wanted['style'], wanted['outfit'], self.db.get_edit_choice('style_art')[1],
                                          room=wanted.get('room'), area=wanted.get('area'))
        with self.db.connect(write=True) as conn:
            asset_id = project_art_asset(conn, task['project_id'], art, utc_now())
            result = [*references, asset_id]
            conn.execute('UPDATE generation_tasks SET reference_image_ids=? WHERE id=?', (json.dumps(result), task['id']))
        return result

    async def ensure_style_art(self, style_id: str, outfit_id: str, model: str, *, room: str | None = None,
                               area: int | None = None, fresh: bool = False) -> dict:
        """The style's ready art, made now when there is none: the outfit on white and the empty scene from words,
        then the two put together (docs/prompts.md section 12). One at a time per style and outfit; shots waiting
        for it share it. A failed attempt keeps the steps it finished, and the next attempt starts from them.
        room: the art of a room (V7.34; one per room since V7.50, area is ignored): its empty scene is the room's main
        picture, and the outfit on white is the one the outfit's other arts use (made here when there is none), so
        making it is one edit. fresh: a new art even when one is ready (the user asked to make it again); a room's
        art still keeps the shared outfit picture, so that every room wears the same clothes."""
        area = art_area(room, area)  # V7.50: one art per room, of its main picture
        what = f'{style_id} {room}' if room is not None else style_id
        lock = self._styling_locks.setdefault(('style_art', style_id, outfit_id, room, area), asyncio.Lock())
        async with lock:
            with self.db.connect(write=True) as conn:
                art = None if fresh else current_style_art(conn, style_id, outfit_id, room, area)
                if art is not None and art_files_present(self.config, art):
                    return dict(art)
                if art is not None:
                    logger.warning('Style art files missing, making it again style_id=%s art_id=%s', style_id, art['id'])
                    conn.execute('UPDATE style_arts SET discarded_at=? WHERE id=?', (utc_now(), art['id']))
                # Steps a failed attempt finished are reused, unless the user asked for a new art.
                earlier = None if fresh else failed_style_art(conn, style_id, outfit_id, room, area)
                art_id = new_id()
                kept = {column: earlier[column] if earlier and earlier[column] and asset_path(self.config, earlier[column]).is_file() else None
                        for column in ('product_path', 'scene_path')}
                if room is not None:
                    kept['product_path'] = shared_product(conn, self.config, outfit_id)
                aspect = STYLE_ART_COMBINE_ASPECT_RATIO
                conn.execute("INSERT INTO style_arts (id,style_id,outfit_id,art_version,status,model,product_path,scene_path,created_at,"
                             "room_id,area,scene_version,aspect_ratio) VALUES (?,?,?,?,'running',?,?,?,?,?,?,?,?)",
                             (art_id, style_id, outfit_id, style_art_version(outfit_id), model, kept['product_path'], kept['scene_path'], utc_now(),
                              room, area, room_image_version(style_id, room) if room is not None else None, aspect))
            try:
                paths = {}
                if room is not None and not kept['scene_path']:
                    # The room's main picture is the empty scene: copied into the art's folder.
                    content = await asyncio.to_thread(main_image(style_id, room).read_bytes)
                    kept['scene_path'] = await asyncio.to_thread(save_style_art_image, self.config, art_id, 'scene', content)
                    with self.db.connect(write=True) as conn:
                        conn.execute('UPDATE style_arts SET scene_path=? WHERE id=?', (kept['scene_path'], art_id))
                if not kept['product_path']:
                    # The first art of an outfit makes its outfit picture; arts of the outfit's other rooms made at the
                    # same time wait for it and take it, rather than each making clothes of their own.
                    async with self._styling_locks.setdefault(('style_art_product', outfit_id), asyncio.Lock()):
                        if room is not None:
                            with self.db.connect() as conn:
                                kept['product_path'] = shared_product(conn, self.config, outfit_id)
                        if not kept['product_path']:
                            content = await self._outfit_picture(outfit_id, model, what)
                            kept['product_path'] = await asyncio.to_thread(save_style_art_image, self.config, art_id, 'product', content)
                        with self.db.connect(write=True) as conn:
                            conn.execute('UPDATE style_arts SET product_path=? WHERE id=?', (kept['product_path'], art_id))
                paths['product_path'] = kept['product_path']
                if kept['scene_path']:
                    paths['scene_path'] = kept['scene_path']
                else:
                    content = await self._style_art_request([], style_art_scene_prompt(style_id), model, f'{what} scene')
                    paths['scene_path'] = await asyncio.to_thread(save_style_art_image, self.config, art_id, 'scene', content)
                    with self.db.connect(write=True) as conn:
                        conn.execute('UPDATE style_arts SET scene_path=? WHERE id=?', (paths['scene_path'], art_id))
                # The two pictures go in as smaller copies, as the art was tested.
                inputs = []
                for column, name in (('product_path', 'product_ref'), ('scene_path', 'scene_ref')):
                    content = await asyncio.to_thread(asset_path(self.config, paths[column]).read_bytes)
                    copy = await asyncio.to_thread(reference_copy, content)
                    inputs.append(asset_path(self.config, await asyncio.to_thread(save_style_art_image, self.config, art_id, name, copy)))
                # V7.52: the art is always 3:2 (V7.49 gave it the scene's shape, 16:9 for a room).
                combined = await self._style_art_request(inputs, STYLE_ART_COMBINE_PROMPT, model, f'{what} combine', aspect)
                image_path = await asyncio.to_thread(save_style_art_image, self.config, art_id, 'art',
                                                     await asyncio.to_thread(reference_copy, combined))
            except GeneratorError as error:
                self._style_art_failed(art_id, error.error_code, error.message)
                raise
            except BaseException:
                self._style_art_failed(art_id, 'generation_failed', '服饰场景图生成失败，请稍后重试')
                raise
            with self.db.connect(write=True) as conn:
                conn.execute("UPDATE style_arts SET status='ready',image_path=?,finished_at=? WHERE id=?",
                             (image_path, utc_now(), art_id))
                if fresh:
                    conn.execute('UPDATE style_arts SET discarded_at=? WHERE style_id=? AND outfit_id=? AND room_id IS ? AND area IS ? '
                                 'AND id<>? AND discarded_at IS NULL', (utc_now(), style_id, outfit_id, room, area, art_id))
                return dict(conn.execute('SELECT * FROM style_arts WHERE id=?', (art_id,)).fetchone())

    async def _outfit_picture(self, outfit_id: str, model: str, what: str) -> bytes:
        """The outfit on light gray (V7.109): an outfit with a checked picture in the repository (V7.35, on white) has
        it turned gray by one edit, so the checked clothes stay; any other outfit is made from words."""
        checked = outfit_product_image(outfit_id)
        if checked is not None:
            return await self._style_art_request([checked], STYLE_ART_GRAY_PROMPT, model, f'{what} product gray')
        return await self._style_art_request([], style_art_product_prompt(outfit_id), model, f'{what} product')

    async def _style_art_request(self, images: list, prompt: str, model: str, what: str,
                                 aspect_ratio: str = STYLE_ART_ASPECT_RATIO) -> bytes:
        started = time.monotonic()
        logger.info('Style art request started model=%s %s aspect=%s', model, what, aspect_ratio)
        provider = self.db.get_edit_provider('style_art')
        content = await asyncio.to_thread(self.stylist.generate_image, images, prompt, model, aspect_ratio,
                                          **({'provider': provider} if provider else {}))
        logger.info('Style art request succeeded model=%s %s elapsed_seconds=%.3f', model, what, time.monotonic() - started)
        return content

    def _style_art_failed(self, art_id: str, code: str, message: str):
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE style_arts SET status='failed',error_code=?,error_message=?,finished_at=? WHERE id=?",
                         (code, message, utc_now(), art_id))

    def request_style_art(self, style_id: str, outfit_id: str, model: str, room: str | None = None, area: int | None = None):
        """Make the style's art (of a room area, V7.34) again in the background (the user asked for it); a failure
        stays on its row."""
        async def run():
            try:
                await self.ensure_style_art(style_id, outfit_id, model, room=room, area=area, fresh=True)
            except Exception:
                logger.warning('Style art failed style_id=%s', style_id, exc_info=True)
        future = asyncio.run_coroutine_threadsafe(run(), self._loop)
        return future

    def _size_warning(self, content: bytes, model: str, aspect_ratio: str, what: str,
                      resolution: str | None = None) -> str | None:
        """The service returned another size than requested; the picture is kept, and the problem is logged."""
        if self.config.generator == 'mock':
            return None
        problem = size_problem(content, model, aspect_ratio, resolution or self.config.resolution)
        if problem is not None:
            logger.warning('Image size mismatch model=%s %s: %s', model, what, problem)
        return problem

    async def _look_edit(self, look: dict, images: list, prompt: str, model: str, step: str, kind: str = 'look') -> bytes:
        """An edit of a look, or of a makeup (kind 'makeup'), sent to that step's service in Settings (2026-11-13)."""
        started = time.monotonic()
        logger.info('Look request started model=%s look_id=%s step=%s', model, look['id'], step)
        provider = self.db.get_edit_provider(kind)
        content = await asyncio.to_thread(self.stylist.generate_image, images, prompt, model, STYLING_ASPECT_RATIO,
                                          **({'provider': provider} if provider else {}))
        self._size_warning(content, model, STYLING_ASPECT_RATIO, f"look_id={look['id']} step={step}")
        logger.info('Look request succeeded model=%s look_id=%s step=%s elapsed_seconds=%.3f',
                    model, look['id'], step, time.monotonic() - started)
        return content

    async def _save_look_image(self, look: dict, content: bytes) -> str:
        asset_id = new_id()
        relative_path = await asyncio.to_thread(save_image, self.config, look['project_id'], asset_id, content)
        with self.db.connect(write=True) as conn:
            conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at,source_asset_id) '
                         "VALUES (?,?,?,'styled',?,?)", (asset_id, look['project_id'], relative_path, utc_now(), look['source_asset_id']))
        return asset_id

    async def _execute_look(self, look: dict):
        """Edit an uploaded photo into a look candidate the user asked for, kept as it comes back (V7.0).

        Looks made before V7.22 for the groom are one edit (hair and grooming); since then his front look is two, as
        the bride's, on his selected retouched photo. The bride's front look is two (V7.5): makeup, skin and
        background on the uploaded photo, then only the hair on that made-up photo; a retry after a failed hair edit
        starts from the saved made-up photo. Since V7.8 the made-up photo is the style's selected makeup candidate,
        given to the look when it is created, so the look itself is only the hair edit. Likeness scores then pick the
        most similar look of its style.
        """
        if look['angle'] != 'front':
            await self._execute_side_look(look)
            return
        model = look['model'] or self.db.get_edit_choice('look')[1]
        with self.db.connect() as conn:
            source = self._asset_file(conn, look['source_asset_id'])
            makeup = self._asset_file(conn, look['makeup_asset_id']) if look['makeup_asset_id'] else None
        if source is None:
            self._fail_look(look, 'reference_missing', '这个人的照片文件缺失或无法读取，请重新上传后重试')
            return
        if look['makeup_id'] and makeup is None:
            # Never make the makeup again here: the look's hair candidates are meant to share this one.
            self._fail_look(look, 'reference_missing', '这套造型用的妆容图片缺失，请重新生成妆容后再做发型')
            return
        try:
            if look['hair_prompt']:
                if makeup is None:
                    content = await self._look_edit(look, [source], look['prompt'], model, 'makeup')
                    makeup_id = await self._save_look_image(look, content)
                    with self.db.connect(write=True) as conn:
                        conn.execute('UPDATE looks SET makeup_asset_id=?,makeup_face_score=NULL WHERE id=?', (makeup_id, look['id']))
                        makeup = self._asset_file(conn, makeup_id)
                content = await self._look_edit(look, [makeup], look['hair_prompt'], model, 'hair')
            else:
                content = await self._look_edit(look, [source], look['prompt'], model, 'look')
        except GeneratorError as error:
            self._candidate_edit_failed('looks', look, error)
            return
        except OSError:
            logger.exception('Look storage failed for look %s', look['id'])
            self._fail_look(look, 'storage_error', f'图片保存失败，请检查存储空间及目录权限：{self.config.data_dir}')
            return
        except Exception:
            logger.exception('Unexpected look failure for look %s', look['id'])
            self._fail_look(look, 'generation_failed', '造型生成失败，请稍后重试')
            return
        try:
            asset_id = await self._save_look_image(look, content)
            now = utc_now()
            with self.db.connect(write=True) as conn:
                conn.execute("UPDATE looks SET status='ready',image_asset_id=?,model=?,error_code=NULL,error_message=NULL,"
                             "face_score=NULL,finished_at=? WHERE id=? AND status='running'", (asset_id, model, now, look['id']))
                conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, look['project_id']))
                ready = conn.execute("SELECT * FROM looks WHERE id=? AND status='ready'", (look['id'],)).fetchone()
                project = conn.execute('SELECT * FROM projects WHERE id=?', (look['project_id'],)).fetchone()
                if ready is not None:
                    auto_select(conn, project, ready)
                    self._queue_face_score(conn, 'looks', look['id'])
            self._rate_limit_count = 0
        except OSError:
            logger.exception('Look storage failed for look %s', look['id'])
            self._fail_look(look, 'storage_error', f'图片保存失败，请检查存储空间及目录权限：{self.config.data_dir}')
        except Exception:
            logger.exception('Look persistence failed for look %s', look['id'])
            self._fail_look(look, 'save_failed', '造型保存失败，请检查本地数据后重试')

    async def _execute_side_look(self, look: dict):
        """A side look the user asked for in the look step (V7.30): one edit of the side photo with the prompt stored
        when it was queued (the same edit a shot makes when it needs one, V5.0)."""
        model = look['model'] or self.db.get_edit_choice('look')[1]
        with self.db.connect() as conn:
            key = side_look_key(conn, look['parent_look_id'], look['angle'], look['source_asset_id'])
        async with self._styling_locks.setdefault(key, asyncio.Lock()):
            with self.db.connect(write=True) as conn:
                made = find_side_look(conn, look['parent_look_id'], look['angle'], look['source_asset_id'])
                if made is not None and self._asset_file(conn, made) is not None:
                    # A shot made it while this one was queued: use that image, no second paid edit.
                    conn.execute("UPDATE looks SET status='ready',image_asset_id=?,finished_at=? WHERE id=? AND status='running'",
                                 (made, utc_now(), look['id']))
                    self._queue_face_score(conn, 'looks', look['id'])
                    return
                # V7.46: the side photo alone, styled by the prompt stored when it was queued.
                paths = [self._asset_file(conn, look['source_asset_id'])]
            if None in paths:
                self._fail_look(look, 'reference_missing', '侧脸照的文件缺失，请重新上传后再试')
                return
            try:
                content = await self._edit_side_look(paths, look['prompt'], model, f"look_id={look['id']} angle={look['angle']}")
            except GeneratorError as error:
                self._candidate_edit_failed('looks', look, error)
                return
            except Exception:
                logger.exception('Unexpected side look failure for look %s', look['id'])
                self._fail_look(look, 'generation_failed', '侧面造型生成失败，请稍后重试')
                return
            try:
                asset_id = new_id()
                relative_path = await asyncio.to_thread(save_image, self.config, look['project_id'], asset_id, content)
                now = utc_now()
                with self.db.connect(write=True) as conn:
                    conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at,source_asset_id) '
                                 "VALUES (?,?,?,'styled',?,?)", (asset_id, look['project_id'], relative_path, now, look['source_asset_id']))
                    conn.execute("UPDATE looks SET status='ready',image_asset_id=?,model=?,error_code=NULL,error_message=NULL,"
                                 "face_score=NULL,finished_at=? WHERE id=? AND status='running'", (asset_id, model, now, look['id']))
                    conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, look['project_id']))
                    self._queue_face_score(conn, 'looks', look['id'])
                self._rate_limit_count = 0
            except Exception:
                logger.exception('Side look persistence failed for look %s', look['id'])
                self._fail_look(look, 'save_failed', '侧面造型保存失败，请检查本地数据后重试')

    async def _edit_side_look(self, paths: list, prompt: str, model: str, context: str) -> bytes:
        """Edit a side photo into its front look's styling (V5.0), from words only since V7.46."""
        started = time.monotonic()
        logger.info('Side look request started model=%s %s', model, context)
        provider = self.db.get_edit_provider('look')
        content = await asyncio.to_thread(self.stylist.generate_image, paths, prompt, model, STYLING_ASPECT_RATIO,
                                          **({'provider': provider} if provider else {}))
        self._size_warning(content, model, STYLING_ASPECT_RATIO, f'{context} side_look')
        logger.info('Side look request succeeded model=%s %s elapsed_seconds=%.3f', model, context, time.monotonic() - started)
        return content

    def _candidate_edit_failed(self, table: str, row: dict, error: GeneratorError):
        """A refused or lost edit of a look or makeup candidate: queued again after a rate limit, else failed."""
        logger.warning('Generator outcome %s for %s %s', error.kind, table, row['id'])
        if error.kind == OutcomeKind.RATE_LIMITED:
            self._rate_limit_count += 1
            delay = error.retry_after
            if delay is None or not math.isfinite(delay) or delay < 0:
                delay = min(60.0, 2.0 ** min(self._rate_limit_count, 6))
            self._backoff_until = max(self._backoff_until, time.monotonic() + max(0.05, delay))
            with self.db.connect(write=True) as conn:
                conn.execute(f"UPDATE {table} SET status='waiting' WHERE id=? AND status='running'", (row['id'],))
            return
        self._fail_look(row, error.error_code, error.message,
                        status='interrupted' if error.kind == OutcomeKind.UNKNOWN_OUTCOME else 'failed', table=table)

    def _fail_look(self, look: dict, code: str, message: str, *, status: str = 'failed', table: str = 'looks'):
        with self.db.connect(write=True) as conn:
            conn.execute(f"UPDATE {table} SET status=?,error_code=?,error_message=?,finished_at=? WHERE id=? AND status='running'",
                         (status, code, message, utc_now(), look['id']))
            conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), look['project_id']))

    async def _execute_makeup(self, makeup: dict):
        """Edit the bride's uploaded photo into a makeup candidate for one style, hair unchanged (V7.8), or the
        groom's into a retouched photo for every style (V7.22); the most similar one of its group is then picked
        unless the user picked one."""
        model = makeup['model'] or self.db.get_edit_choice('makeup')[1]
        with self.db.connect() as conn:
            source = self._asset_file(conn, makeup['source_asset_id'])
        if source is None:
            who = '新娘' if makeup['role'] == 'bride' else '新郎'
            self._fail_look(makeup, 'reference_missing', f'{who}的照片文件缺失或无法读取，请重新上传后重试', table='makeups')
            return
        try:
            content = await self._look_edit(makeup, [source], makeup['prompt'], model, 'makeup', kind='makeup')
            asset_id = await self._save_look_image(makeup, content)
            now = utc_now()
            with self.db.connect(write=True) as conn:
                conn.execute("UPDATE makeups SET status='ready',image_asset_id=?,model=?,error_code=NULL,error_message=NULL,"
                             "face_score=NULL,finished_at=? WHERE id=? AND status='running'", (asset_id, model, now, makeup['id']))
                conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, makeup['project_id']))
                ready = conn.execute("SELECT * FROM makeups WHERE id=? AND status='ready'", (makeup['id'],)).fetchone()
                project = conn.execute('SELECT * FROM projects WHERE id=?', (makeup['project_id'],)).fetchone()
                if ready is not None:
                    auto_select_makeup(conn, project, ready)
                    self._queue_face_score(conn, 'makeups', makeup['id'])
            self._rate_limit_count = 0
        except GeneratorError as error:
            self._candidate_edit_failed('makeups', makeup, error)
        except OSError:
            logger.exception('Makeup storage failed for makeup %s', makeup['id'])
            self._fail_look(makeup, 'storage_error', f'图片保存失败，请检查存储空间及目录权限：{self.config.data_dir}', table='makeups')
        except Exception:
            logger.exception('Unexpected makeup failure for makeup %s', makeup['id'])
            self._fail_look(makeup, 'generation_failed', '妆容生成失败，请稍后重试', table='makeups')

    async def _execute_makeup_scoring(self, makeup_ids: list[str]):
        """Likeness of each makeup candidate to the uploaded photo; the scores pick the most similar makeup of its
        style unless the user picked one."""
        try:
            items, scored = [], []
            with self.db.connect() as conn:
                for makeup_id in makeup_ids:
                    makeup = conn.execute('SELECT * FROM makeups WHERE id=?', (makeup_id,)).fetchone()
                    source = self._asset_file(conn, makeup['source_asset_id']) if makeup else None
                    target = self._asset_file(conn, makeup['image_asset_id']) if makeup and makeup['image_asset_id'] else None
                    if source is not None and target is not None:
                        items.append(ScoreItem(source, source, target))
                        scored.append((makeup_id, makeup['role']))
            if not items:
                return
            scores = await asyncio.to_thread(self.swapper.score, items)
            with self.db.connect(write=True) as conn:
                for (makeup_id, role), value in zip(scored, scores):
                    # One person per picture: the face of the other gender is not found (V7.22: the groom's too).
                    if value is not None and value.get(role) is not None:
                        conn.execute('UPDATE makeups SET face_score=? WHERE id=? AND face_score IS NULL', (value[role], makeup_id))
                        # Looks made on it before it was scored show its score too.
                        conn.execute('UPDATE looks SET makeup_face_score=? WHERE makeup_id=? AND makeup_face_score IS NULL',
                                     (value[role], makeup_id))
                for makeup_id, _role in scored:
                    makeup = conn.execute('SELECT * FROM makeups WHERE id=?', (makeup_id,)).fetchone()
                    project = conn.execute('SELECT * FROM projects WHERE id=?', (makeup['project_id'],)).fetchone()
                    if project is not None:
                        auto_select_makeup(conn, project, makeup)
        except Exception:
            logger.warning('Makeup scoring failed for %s makeups', len(makeup_ids), exc_info=True)
        finally:
            self._finish_face_scoring('makeups', makeup_ids)

    async def _execute_look_scoring(self, look_ids: list[str]):
        """Likeness of each look (and of a bride look's made-up step, V7.5) to the uploaded photo it was made from;
        the scores then pick the most similar look of its style unless the user picked one. A side look (V7.45) is
        compared with the side photo it was made from and only shows its score."""
        try:
            items, scored = [], []
            with self.db.connect() as conn:
                for look_id in look_ids:
                    look = conn.execute('SELECT * FROM looks WHERE id=?', (look_id,)).fetchone()
                    if look is None or look['image_asset_id'] is None:
                        continue
                    source = self._asset_file(conn, look['source_asset_id'])
                    for column, asset_id in (('face_score', look['image_asset_id']), ('makeup_face_score', look['makeup_asset_id'])):
                        # A look made on a makeup candidate (V7.8) gets that candidate's score instead.
                        if column == 'makeup_face_score' and look['makeup_id']:
                            continue
                        target = self._asset_file(conn, asset_id) if asset_id else None
                        if source is not None and target is not None:
                            # One person per picture: the other role finds no face of its gender and stays empty.
                            items.append(ScoreItem(source, source, target))
                            scored.append((look_id, look['role'], column))
            if not items:
                return
            scores = await asyncio.to_thread(self.swapper.score, items)
            with self.db.connect(write=True) as conn:
                for (look_id, role, column), value in zip(scored, scores):
                    if value is not None and value.get(role) is not None:
                        conn.execute(f'UPDATE looks SET {column}=? WHERE id=? AND {column} IS NULL', (value[role], look_id))
                for look_id in dict.fromkeys(look_id for look_id, _role, _column in scored):
                    look = conn.execute('SELECT * FROM looks WHERE id=?', (look_id,)).fetchone()
                    if look is None or look['angle'] != 'front':
                        continue  # a side look (V7.45) is only shown its score; it is not a candidate to pick
                    project = conn.execute('SELECT * FROM projects WHERE id=?', (look['project_id'],)).fetchone()
                    if project is not None:
                        auto_select(conn, project, look)
        except Exception:
            logger.warning('Look scoring failed for %s looks', len(look_ids), exc_info=True)
        finally:
            self._finish_face_scoring('looks', look_ids)

    def _asset_file(self, conn, asset_id: str):
        """Path of an image asset's file; None when the asset or its file is missing."""
        row = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (asset_id,)).fetchone()
        path = asset_path(self.config, row['file_path']) if row else None
        return path if path is not None and path.is_file() else None

    async def _execute_face_swap(self, task: dict):
        try:
            if not await self._prepare_groom_swap(task):
                return
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
            swap_snapshot = json.loads(task['model_config_snapshot'])
            weight = swap_snapshot['weight']
            # V7.13: the groom only, after the bride's AI face fix; both people otherwise.
            roles = tuple(swap_snapshot.get('roles') or ('bride', 'groom'))
            result = await asyncio.to_thread(self.swapper.swap, bride, groom, target_path, weight, roles)
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
            # The swap measures likeness against its own fronts, the picture's looks: the same as every picture's
            # score (V7.33), so it keeps what it measured before and after.
            self._commit_result(task, asset_id, relative_path, result.scores)
        except TaskStopped:
            logger.info('Result discarded for stopped task %s', task['id'])
        except OSError:
            self._fail(task, 'storage_error', '图片保存失败，请检查存储空间及目录权限', hold=False)
        except Exception:
            logger.exception('Face-swap result persistence failed for task %s', task['id'])
            self._fail(task, 'save_failed', '换脸结果保存失败，请稍后重试', hold=False)

    async def _execute_face_fix(self, task: dict):
        """The AI face fix (V7.1; the whole picture since V7.2): one edit of the source version with both looks (V7.4),
        kept as it comes back as the slot's new version (docs/prompts.md section 9). A hand fix (V7.25) is the same
        edit with the picture alone and its own prompt, and so is a wash (V7.26), asked for in 4K."""
        snapshot = self._snapshot(task)
        model = snapshot.get('model') or self.db.get_default_model() or DEFAULT_MODEL
        mode = task['regenerate_mode']
        what = {'hand_fix': '修手', 'wash': '洗图', 'regional_edit': '局部编辑'}.get(mode, 'AI 修脸')
        resolution = snapshot.get('resolution')
        try:
            with self.db.connect() as conn:
                references = json.loads(task['reference_image_ids'])
                # A one-person fix (V7.13 the bride, V7.29 either) sends only that person's look; both stay recorded
                # for the likeness scores. A hand fix or wash sends none: the references only score the result.
                wanted = [] if mode in ('hand_fix', 'wash', 'regional_edit') else [references[('bride', 'groom').index(role)] for role in fix_roles(snapshot)]
                count = len(wanted)
                photos = [self._asset_file(conn, asset_id) for asset_id in wanted]
                source_version = conn.execute('SELECT image_asset_id FROM photo_versions WHERE id=? AND photo_slot_id=?',
                                              (task['source_version_id'], task['target_slot_id'])).fetchone()
                target_path = self._asset_file(conn, source_version['image_asset_id']) if source_version else None
                annotation_path = self._asset_file(conn, snapshot['annotation_asset_id']) if mode == 'regional_edit' else None
            if target_path is None or len(photos) < count or None in photos:
                raise FileNotFoundError('Face-fix inputs are incomplete')
            if mode == 'regional_edit' and annotation_path is None:
                raise FileNotFoundError('Annotation is missing')
        except Exception:
            logger.warning('Face-fix inputs unavailable for task %s', task['id'])
            self._fail(task, 'reference_missing', '参考照片文件缺失或无法读取，请重新上传后重试')
            return
        try:
            started = time.monotonic()
            logger.info('Face-fix request started model=%s task_id=%s mode=%s', model, task['id'], mode)
            aspect_ratio = snapshot.get('aspect_ratio') or self.config.aspect_ratio
            extra = {'resolution': resolution} if resolution else {}
            if snapshot.get('provider'):
                extra['provider'] = snapshot['provider']  # V7.27: the service chosen for this kind of edit
            # V7.79: a wash keeps the prompt written for its picture; washes before it have none and use V7.26's.
            prompt = task['prompt'] if mode == 'wash' and task['prompt'] else fix_prompt(mode, snapshot)
            if mode == 'regional_edit' and snapshot['annotation_kind'] == 'crop':
                content = await self._crop_edit(task, target_path, annotation_path, model, extra.get('provider'))
            else:
                if mode == 'regional_edit':
                    extra['edit'] = RegionalEdit(annotation_path, snapshot['annotation_kind'])
                    prompt = regional_edit_prompt(task['prompt'], snapshot['annotation_kind'])
                content = await asyncio.to_thread(self.generator.generate_image, [target_path, *photos], prompt,
                                                  model, aspect_ratio, **extra)
            if mode == 'regional_edit' and snapshot['annotation_kind'] != 'crop':
                try:
                    content = await asyncio.to_thread(png_result, content)
                except ValueError as exc:
                    raise GeneratorError(OutcomeKind.REJECTED, str(exc), error_code='invalid_edit_result') from None
            # Auto-sized regional edits did not request the normal generation dimensions.
            size_warning = None if mode == 'regional_edit' else self._size_warning(
                content, model, aspect_ratio, f"task_id={task['id']}", resolution)
            logger.info('Face-fix request succeeded model=%s task_id=%s elapsed_seconds=%.3f',
                        model, task['id'], time.monotonic() - started)
        except GeneratorError as error:
            self._handle_generator_error(task, error)
            return
        except Exception:
            logger.exception('Unexpected face-fix failure for task %s', task['id'])
            self._fail(task, 'face_fix_failed', f'{what}失败，已保留原图，请稍后重试')
            return
        try:
            asset_id = new_id()
            relative_path = await asyncio.to_thread(save_image, self.config, task['project_id'], asset_id, content)
            self._commit_result(task, asset_id, relative_path, size_warning=size_warning)
            self._rate_limit_count = 0
        except TaskStopped:
            logger.info('Result discarded for stopped task %s', task['id'])
        except OSError:
            logger.exception('Face-fix storage failed for task %s', task['id'])
            self._fail(task, 'storage_error', f'图片保存失败，请检查存储空间及目录权限：{self.config.data_dir}', hold=True)
        except Exception:
            logger.exception('Face-fix result persistence failed for task %s', task['id'])
            self._fail(task, 'save_failed', f'{what}结果保存失败，请检查本地数据后重试', hold=True)

    async def _crop_edit(self, task: dict, source: Path, annotation: Path, model: str, provider: str | None) -> bytes:
        """V7.79: edit the enlarged marked area (a 1024x1024 square, '1:1' at 1K) and lay only the marked area back."""
        try:
            crop = await asyncio.to_thread(crop_edit_inputs, source, annotation)
        except (OSError, ValueError) as exc:
            raise GeneratorError(OutcomeKind.REJECTED, '无法裁出标记区域，请重新打开编辑器标记', error_code='invalid_annotation') from exc
        with tempfile.TemporaryDirectory(prefix='vowframe-crop-') as folder:
            clean, marked = Path(folder) / 'crop.png', Path(folder) / 'crop-marked.png'
            clean.write_bytes(crop.clean)
            marked.write_bytes(crop.marked)
            extra = {'provider': provider} if provider else {}
            content = await asyncio.to_thread(self.generator.generate_image, [clean, marked], crop_edit_prompt(task['prompt']),
                                              model, '1:1', resolution='1K', **extra)
        try:
            return await asyncio.to_thread(paste_crop_result, source, annotation, crop.box, content)
        except ValueError as exc:
            raise GeneratorError(OutcomeKind.REJECTED, str(exc), error_code='invalid_edit_result') from None

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

    def _auto_face_fix_plan(self, task: dict) -> tuple[list[str], list[str]] | None:
        """What to queue after this task (V7.29, per person): (people the AI face fix corrects, people FaceFusion
        swaps), or None when nothing automatic applies. The AI fix goes first and FaceFusion after it: the AI fix
        redraws the whole picture and would redraw a swapped face too. Without FaceFusion nobody is swapped."""
        if task['regenerate_mode'] in ('face_swap', 'face_fix', 'hand_fix', 'wash', 'regional_edit') or task['moment_shot_size'] == DETAIL_SHOT:
            return None  # never fix a fix; detail shots have no faces
        if task['moment_shot_size'] and is_faceless_shot(task['prompt']):
            return None  # back views and silhouettes show no faces; a manual fix is still available
        methods = self.db.get_auto_face_fix_roles()
        ai = [role for role in ('bride', 'groom') if methods[role] == 'ai']
        # The swap needs FaceFusion; the AI fix does not (V7.2).
        swap = [role for role in ('bride', 'groom') if methods[role] == 'facefusion'] if self.swapper.status['available'] else []
        return (ai, swap) if ai or swap else None

    async def _execute_scoring(self, version_ids: list[str]):
        try:
            items, scored = [], []
            with self.db.connect() as conn:
                for version_id in version_ids:
                    row = conn.execute(
                        'SELECT asset.file_path,task.reference_image_ids FROM photo_versions version '
                        'JOIN image_assets asset ON asset.id=version.image_asset_id '
                        'JOIN generation_tasks task ON task.id=version.generation_task_id '
                        'WHERE version.id=? AND version.face_scores IS NULL',
                        (version_id,),
                    ).fetchone()
                    if row is None:
                        continue  # already scored, or deleted while waiting
                    # Every task keeps [bride front, groom front] first: the looks the picture was made from, or the
                    # uploaded photos before looks existed. A picture is compared with them (V7.33; the uploaded
                    # photos V4.0-V7.32): how far each look is from the person is the look's own score.
                    references = json.loads(row['reference_image_ids'])[:2]
                    paths = [conn.execute('SELECT file_path FROM image_assets WHERE id=?', (asset_id,)).fetchone()
                             for asset_id in references]
                    if len(paths) < 2 or None in paths:
                        continue
                    bride, groom, target = (asset_path(self.config, item['file_path']) for item in (*paths, row))
                    if bride.is_file() and groom.is_file() and target.is_file():
                        items.append(ScoreItem(bride, groom, target))
                        scored.append(version_id)
            if items:
                scores = await asyncio.to_thread(self.swapper.score, items)
                with self.db.connect(write=True) as conn:
                    for version_id, value in zip(scored, scores):
                        if value is not None:
                            conn.execute('UPDATE photo_versions SET face_scores=? WHERE id=? AND face_scores IS NULL',
                                         (json.dumps(value), version_id))
            with self.db.connect(write=True) as conn:
                for version_id in version_ids:
                    self._accept_face_fix(conn, version_id)
        except Exception:
            # A score is only shown to help judge the picture; never retried in a loop, the next start tries again.
            logger.warning('Face scoring failed for %s versions', len(version_ids), exc_info=True)
        finally:
            self._finish_face_scoring('photo_versions', version_ids)

    async def _prepare_groom_swap(self, task: dict) -> bool:
        """Judge a one-person AI fix in the face lane before FaceFusion swaps the other person (V7.13 the groom after
        the bride; V7.29 either way round). The swap is already a persisted task, so scoring cannot leave the batch
        completed, escape cancellation or lose the follow-up on restart."""
        snapshot = self._snapshot(task)
        if not snapshot.get('roles'):
            return True  # a swap of both people is never a follow-up of a one-person fix
        with self.db.connect() as conn:
            fix = conn.execute(
                'SELECT source.source_version_id,source.model_config_snapshot FROM photo_versions version '
                'JOIN generation_tasks source ON source.id=version.generation_task_id '
                "WHERE version.id=? AND version.photo_slot_id=? AND source.regenerate_mode='face_fix'",
                (task['source_version_id'], task['target_slot_id'])).fetchone()
        allowed_sources = {task['source_version_id']}
        if fix is not None and json.loads(fix['model_config_snapshot']).get('roles'):
            allowed_sources.add(fix['source_version_id'])
            await self._execute_scoring([fix['source_version_id'], task['source_version_id']])
        with self.db.connect(write=True) as conn:
            current = conn.execute('SELECT status FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
            if current is None or current['status'] != 'running':
                return False
            slot = conn.execute('SELECT current_version_id FROM photo_slots WHERE id=?', (task['target_slot_id'],)).fetchone()
            if slot is None or slot['current_version_id'] not in allowed_sources:
                conn.execute("UPDATE generation_tasks SET status='cancelled',error_code='source_changed',"
                             'error_message=?,finished_at=? WHERE id=?',
                             ('作品已切换到其他版本，已取消这次自动换脸', utc_now(), task['id']))
                return False
            task['source_version_id'] = slot['current_version_id']
            conn.execute('UPDATE generation_tasks SET source_version_id=? WHERE id=?',
                         (task['source_version_id'], task['id']))
        return True

    def _queue_groom_swap(self, conn, version_id: str) -> bool:
        """Persist FaceFusion's follow-up (V7.13 the groom, V7.29 whoever chose it) in the same transaction as the AI
        fix that finished. Its input is judged in the face lane before swapping; queued once, in the fix's batch,
        respecting a batch pause."""
        row = conn.execute(
            'SELECT version.photo_slot_id,task.id AS task_id,task.batch_id,task.source_version_id,task.model_config_snapshot '
            'FROM photo_versions version JOIN generation_tasks task ON task.id=version.generation_task_id '
            "WHERE version.id=? AND task.regenerate_mode='face_fix'", (version_id,)).fetchone()
        if row is None:
            return False
        snapshot = json.loads(row['model_config_snapshot'])
        swap_roles = follow_up_swap_roles(snapshot)
        if not swap_roles or snapshot.get('groom_swap_queued') or not self.swapper.status['available']:
            return False
        slot = conn.execute('SELECT project_id,kind,current_version_id FROM photo_slots WHERE id=?', (row['photo_slot_id'],)).fetchone()
        if slot is None or slot['current_version_id'] not in (version_id, row['source_version_id']):
            return False  # the slot moved on (restored, regenerated): nothing to add the groom to
        shot = conn.execute(
            'SELECT task.* FROM photo_versions version JOIN generation_tasks task ON task.id=version.generation_task_id '
            'WHERE version.id=?', (row['source_version_id'],)).fetchone()
        project = conn.execute('SELECT * FROM projects WHERE id=?', (slot['project_id'],)).fetchone()
        if shot is None or project is None:
            return False
        paused = conn.execute('SELECT paused_at FROM generation_batches WHERE id=?', (row['batch_id'],)).fetchone()['paused_at']
        conn.execute(
            'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,'
            'style_id,pose_id,moment_title,moment_shot_size,prompt,model_config_snapshot,regenerate_mode,'
            "source_version_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,'',?,'face_swap',?,?)",
            (new_id(), slot['project_id'], row['batch_id'], row['photo_slot_id'], 'held' if paused else 'waiting',
             json.dumps(task_face_swap_reference_ids(conn, project, self._snapshot(dict(shot)), json.loads(shot['reference_image_ids']))),
             shot['style_id'], shot['pose_id'], shot['moment_title'], shot['moment_shot_size'],
             face_swap_snapshot(self.swapper.status['mode'], FACE_SWAP_WEIGHT, tuple(swap_roles)),
             slot['current_version_id'], utc_now()))
        snapshot['groom_swap_queued'] = True
        conn.execute('UPDATE generation_tasks SET model_config_snapshot=? WHERE id=?',
                     (json.dumps(snapshot, ensure_ascii=False), row['task_id']))
        self._wake.set()
        return True

    def _accept_face_fix(self, conn, version_id: str):
        """An AI face fix that made either person less like their look (V7.33; their uploaded photo before) does not
        replace the picture it fixed (V7.5): the slot goes back to the original, and the fix stays in the version history. A hand fix
        (V7.25) is judged the same way: it must not change the faces. A wash is not (V7.79, the user: 洗完图没法正常对比，还要去看
        历史版本): it stays the current version with its scores shown, so the 洗图 · 对比 chip compares it with the picture
        before, and the user keeps or restores."""
        row = conn.execute(
            'SELECT version.photo_slot_id,version.face_scores,task.id AS task_id,task.source_version_id,'
            'task.model_config_snapshot FROM photo_versions version JOIN generation_tasks task '
            "ON task.id=version.generation_task_id WHERE version.id=? AND task.regenerate_mode IN ('face_fix','hand_fix')",
            (version_id,)).fetchone()
        if row is None or not row['source_version_id']:
            return
        source = conn.execute('SELECT face_scores FROM photo_versions WHERE id=?', (row['source_version_id'],)).fetchone()
        before = json.loads(source['face_scores']) if source and source['face_scores'] else None
        after = json.loads(row['face_scores']) if row['face_scores'] else None
        if before is None or after is None:
            return  # nothing to compare: the fix stays
        # A bride-only fix (V7.13) is judged on her alone: FaceFusion swaps the groom afterwards.
        roles = json.loads(row['model_config_snapshot']).get('roles') or ('bride', 'groom')
        worse = [role for role in roles if before.get(role) is not None
                 and (after.get(role) is None or after[role] < before[role] - FACE_FIX_TOLERANCE)]
        if not worse:
            return
        changed = conn.execute('UPDATE photo_slots SET current_version_id=? WHERE id=? AND current_version_id=?',
                               (row['source_version_id'], row['photo_slot_id'], version_id)).rowcount
        if changed:
            snapshot = json.loads(row['model_config_snapshot'])
            snapshot['kept_original'] = worse
            conn.execute('UPDATE generation_tasks SET model_config_snapshot=? WHERE id=?',
                         (json.dumps(snapshot, ensure_ascii=False), row['task_id']))
            logger.info('Fix made %s less alike; kept the original version slot_id=%s', '/'.join(worse), row['photo_slot_id'])

    def _commit_result(self, task: dict, asset_id: str, relative_path: str, scores: dict | None = None,
                       size_warning: str | None = None):
        now = utc_now()
        version_id = new_id()
        plan = self._auto_face_fix_plan(task)
        follow_up = None if plan is None else 'face_fix' if plan[0] else 'face_swap'
        groom_swap_queued = False
        with self.db.connect(write=True) as conn:
            current = conn.execute('SELECT status FROM generation_tasks WHERE id=?', (task['id'],)).fetchone()
            if current is None or current['status'] != 'running':
                # Stopped by the user (or the project deleted) while the request was out: drop the file.
                asset_path(self.config, relative_path).unlink(missing_ok=True)
                raise TaskStopped(task['id'])
            slot_kind = conn.execute('SELECT kind FROM photo_slots WHERE id=?', (task['target_slot_id'],)).fetchone()['kind']
            # Like a manual swap, an automatic one uses the project's current photos, side photos included.
            project = conn.execute('SELECT * FROM projects WHERE id=?', (task['project_id'],)).fetchone()
            # A follow-up of a paused batch waits for it to continue (V7.12).
            follow_up_status = 'held' if conn.execute(
                'SELECT paused_at FROM generation_batches WHERE id=?', (task['batch_id'],)).fetchone()['paused_at'] else 'waiting'
            number = conn.execute(
                'SELECT COALESCE(MAX(version_number),0)+1 FROM photo_versions WHERE photo_slot_id=?',
                (task['target_slot_id'],),
            ).fetchone()[0]
            asset_type = 'preview' if task['pose_id'] is None and task['moment_title'] is None else 'photo'
            conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at) VALUES (?,?,?,?,?)',
                         (asset_id, task['project_id'], relative_path, asset_type, now))
            scores = scores or {}
            if task['regenerate_mode'] == 'face_swap' and task['source_version_id']:
                # A swap keeps its source's pixels, and with them what is wrong with its size.
                source = conn.execute('SELECT size_warning FROM photo_versions WHERE id=?', (task['source_version_id'],)).fetchone()
                size_warning = source['size_warning'] if source else None
            conn.execute('INSERT INTO photo_versions (id,photo_slot_id,image_asset_id,generation_task_id,version_number,'
                         'created_at,face_scores,size_warning) VALUES (?,?,?,?,?,?,?,?)',
                         (version_id, task['target_slot_id'], asset_id, task['id'], number, now,
                          json.dumps(scores['after']) if scores.get('after') else None, size_warning))
            if scores.get('before'):
                # The swap measured the picture it started from too; keep a score made earlier.
                conn.execute('UPDATE photo_versions SET face_scores=? WHERE id=? AND face_scores IS NULL',
                             (json.dumps(scores['before']), task['source_version_id']))
            pose_name = task['moment_title'] or (POSE_BY_ID.get(task['pose_id'], {}).get('name') if task['pose_id'] else None)
            update = 'UPDATE photo_slots SET current_version_id=?,pose_id=?,pose_name=?,shot_size=? WHERE id=?'
            values = (version_id, task['pose_id'], pose_name, task['moment_shot_size'], task['target_slot_id'])
            if task['regenerate_mode'] == 'regional_edit':
                # Restoring another version while an edit is running must not be undone by its late result.
                update += ' AND current_version_id=?'
                values += (task['source_version_id'],)
            conn.execute(update, values)
            conn.execute(
                "UPDATE generation_tasks SET status='completed', output_asset_id=?, finished_at=?, "
                'error_code=NULL,error_message=NULL WHERE id=?', (asset_id, now, task['id']),
            )
            if follow_up == 'face_fix':
                fix_provider, fix_model = self.db.get_edit_choice('face')  # 2026-11-13: its own (V7.32: the wash's)
                # Same transaction as the new version, like the swap below.
                conn.execute(
                    'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,'
                    'style_id,pose_id,moment_title,moment_shot_size,prompt,model_config_snapshot,regenerate_mode,'
                    "source_version_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,'',?,'face_fix',?,?)",
                    (new_id(), task['project_id'], task['batch_id'], task['target_slot_id'], follow_up_status,
                     json.dumps(json.loads(task['reference_image_ids'])[:2]),
                     task['style_id'], task['pose_id'], task['moment_title'], task['moment_shot_size'],
                     face_fix_snapshot(fix_model, self._snapshot(task).get('aspect_ratio') or self.config.aspect_ratio,
                                       roles=plan[0], swap_roles=plan[1], provider=fix_provider),
                     version_id, now),
                )
            elif follow_up == 'face_swap':
                # Same transaction as the new version: the swap is queued exactly when the original exists.
                # It joins the task's batch, so the batch finishes only after the swapped image is ready.
                conn.execute(
                    'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,'
                    'style_id,pose_id,moment_title,moment_shot_size,prompt,model_config_snapshot,regenerate_mode,'
                    "source_version_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,'',?,'face_swap',?,?)",
                    (new_id(), task['project_id'], task['batch_id'], task['target_slot_id'], follow_up_status,
                     json.dumps(task_face_swap_reference_ids(conn, project, self._snapshot(task),
                                                            json.loads(task['reference_image_ids']))),
                     task['style_id'], task['pose_id'],
                     task['moment_title'], task['moment_shot_size'],
                     face_swap_snapshot(self.swapper.status['mode'], FACE_SWAP_WEIGHT,
                                        tuple(plan[1]) if len(plan[1]) == 1 else None), version_id, now),
                )
            if task['regenerate_mode'] == 'face_fix':
                groom_swap_queued = self._queue_groom_swap(conn, version_id)
            conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, task['project_id']))
            if (not groom_swap_queued and follow_up != 'face_swap' and task['regenerate_mode'] != 'face_swap'
                    and task['moment_shot_size'] != DETAIL_SHOT):
                # A queued swap measures its source; otherwise score the picture in the background.
                self._queue_face_score(conn, 'photo_versions', version_id)

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
