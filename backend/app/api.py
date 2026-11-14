"""Project, asset and task-control HTTP endpoints."""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from starlette.background import BackgroundTask

from .aspect import PORTRAIT_RATIOS, orientation_of, ratio_for
from .db import BATCH_REFERENCE_MODES, STAGE_KINDS, new_id, utc_now
from .face_fix import face_fix_snapshot, hand_fix_snapshot, wash_snapshot
from .face_swap import face_scores, face_swap_snapshot, FACE_SWAP_WEIGHT
from .hand_analysis import HandAnalysisError, HandAnalysisRecord, analysis_inputs
from .models import DIRECTOR_PROVIDERS, PROVIDERS, get_model, provider_for, provider_models
from .facings import FACINGS, SIDE_FACINGS, available_facings, direction, upload_column, uploaded_side
from .retouch import candidate_retouch_settings, project_retouch_settings
from .regional_edit import (CROP_ONLY_MODELS, MAX_ANNOTATION_BYTES, REGIONAL_EDIT_MODELS, UNSUPPORTED_MARKUP_PROVIDERS,
                            oriented_size, validate_annotation)
from .prompts import pair_looks, compose_look_prompt, OUTFIT_BY_ID, OUTFITS, STYLE_OUTFITS, DEFAULT_PREVIEW_STYLE_IDS, DETAIL_SHOT, HAIRSTYLES, HAIRSTYLE_BY_KEY, MAKEUP_COLORS, RETIRED_STYLE_IDS, POSES, POSE_BY_ID, STYLES, STYLE_BY_ID, STYLE_MAKEUP_COLORS, STYLING_VERSION, UPDO_STYLES, compose_identity_boost_moment_prompt, compose_bride_hair_prompt, JEWELRY, JEWELRY_BY_ID, STYLE_JEWELRY_SUGGESTION, compose_bride_makeup_prompt, compose_groom_hair_prompt, compose_groom_retouch_prompt, compose_identity_boost_prompt, compose_moment_prompt, compose_photo_prompt, compose_preview_prompt, director_scene_count, director_scene_text, has_style_art, is_faceless_shot, scene_from_prompt, shot_text_from_prompt, uses_style_art, WASH_REPAIR_PROMPT
from .director import director_key_problem, DirectorFailed, DirectorInvalidOutput, Moment, SHOT_NAMES, SHOT_SIZES, _shot_requirement, moment_dict, validate_moment, validate_moments
from .schemas import SideLook, StyleArt, Batch, BatchActionCreate, ChangePoseCreate, ChangeShotCreate, ExportCreate, FaceFixCreate, FaceSwapCreate, Look, LookCreate, LookOptions, Makeup, OutfitChoice, MakeupCreate, MakeupReuse, Model, MomentDraft, MomentDraftCreate, MomentPatch, MomentRewrite, PhotoBatchCreate, PhotoSet, PreviewCreate, ProjectCreate, ProjectDetail, ProjectPatch, ProjectSummary, RegenerateCreate, RestoreCreate, Settings, SettingsPatch, Slot, Style, Task, TaskStatus, VersionDetail
from .photo_prep import PhotoRejected
from .storage import asset_path, image_format, save_image
from .rooms import default_room, get_room, has_rooms, main_image, style_rooms
from .style_art import (current_style_art, default_outfit, latest_style_art, ready_art_asset, shared_product,
                        style_art_applies, style_art_snapshot)
from .styling import (LOOK_PROBLEM_MESSAGES, MAKEUP_PROBLEM_MESSAGES, auto_select_makeup, bride_look_color, current_side_looks, default_look_items,
                      discard_side_look, latest_side_look,
                      fits_style, hair_length_of, look_problem, look_response, look_snapshot, makeup_level_of, makeup_problem,
                      makeup_response, makeup_style_of, reuse_makeup, reference_slots, role_of, select_makeup, select_only, selected_makeup, style_looks, style_outfits,
                      task_face_swap_reference_ids, uploaded_asset_id)

router = APIRouter(prefix='/api')
logger = logging.getLogger(__name__)
STATUSES = ('waiting', 'held', 'running', 'completed', 'failed', 'cancelled', 'interrupted')
TASK_FIELDS = tuple(Task.model_fields)
# Versions that only correct the faces of another version, which they record as source_version_id.
FACE_FIX_MODES = ('face_swap', 'face_fix')
# Versions that only correct a picture (no prompt of their own): faces, hands (V7.25), or a wash (V7.26).
FIX_MODES = (*FACE_FIX_MODES, 'hand_fix', 'wash', 'regional_edit')


def _uses_preview_reference(request: Request) -> bool:
    return request.app.state.db.get_batch_reference_mode() == 'with_preview'


def _single_scene(request: Request, style_id: str, outfit: str | None, room: str | None = None) -> bool:
    """Whether every shot of a set stays in one scene: with the sample as a reference (docs/prompts.md 7.2), or with
    the style art, which shows the style's main scene (V7.33). In a room (V7.34) every area has an art of its own,
    so the style art leaves the director all of the room's areas."""
    art = style_art_applies(request.app.state.db, style_id, outfit)
    if room is not None and art:
        return False
    return _uses_preview_reference(request) or art


def _source_room(source, slot_label: str) -> tuple[str | None, int | None]:
    """(room, area) of the picture a new shot starts from (V7.34): a set stays in its sample's room. (None, None)
    for pictures before rooms, whose new shots keep the style's own scenes."""
    snapshot = _parse_snapshot(source['model_config_snapshot'], slot_label)
    room = snapshot.get('room')
    if not isinstance(room, dict) or get_room(source['style_id'], room.get('id')) is None:
        return None, None
    return room['id'], room.get('area')


def _source_scene(style_id: str, prompt: str) -> int | None:
    """The scene number of a stored director shot; a shot from the style art was in the main scene (V7.33)."""
    return scene_from_prompt(style_id, prompt) or (1 if uses_style_art(prompt) else None)


def _add_style_art(conn, request: Request, project_id: str, style_id: str, outfit: str | None,
                   references: list[str], room: str | None = None, area: int | None = None) -> tuple[bool, int]:
    """(whether a new shot is made from its style's art (V7.33), references the shot will send). Appends the art when
    it is ready; a task made before that gets it when it runs (app.style_art.art_pending), and it is counted.
    room, area: the shot's room area (V7.34), whose own art it uses."""
    if not style_art_applies(request.app.state.db, style_id, outfit):
        return False, len(references)
    asset_id = ready_art_asset(conn, request.app.state.config, project_id, style_id, outfit, utc_now(), room, area)
    if asset_id is None:
        return True, len(references) + 1
    references.append(asset_id)
    return True, len(references)


def error(status: int, code: str, message: str):
    raise HTTPException(status, detail={'code': code, 'message': message})


def get_project(conn, project_id: str):
    row = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
    if row is None:
        error(404, 'project_not_found', '项目不存在')
    return row


def get_batch(conn, batch_id: str):
    row = conn.execute('SELECT * FROM generation_batches WHERE id=?', (batch_id,)).fetchone()
    if row is None:
        error(404, 'batch_not_found', '批次不存在')
    return row


def get_task(conn, task_id: str):
    row = conn.execute('SELECT * FROM generation_tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        error(404, 'task_not_found', '任务不存在')
    return row


def get_slot(conn, slot_id: str):
    row = conn.execute('SELECT * FROM photo_slots WHERE id=?', (slot_id,)).fetchone()
    if row is None:
        error(404, 'slot_not_found', '作品位置不存在')
    return row


def task_response(row):
    result = {key: row[key] for key in TASK_FIELDS if key != 'model'}
    if result.get('moment_title') is None and result.get('moment_shot_size') is None:
        result.pop('moment_title', None)
        result.pop('moment_shot_size', None)
    try:
        result['model'] = json.loads(row['model_config_snapshot']).get('model')
    except (TypeError, ValueError, AttributeError):
        result['model'] = None
    return result


def version_response(row):
    if row is None:
        return None
    result = dict(row)
    result['face_scores'] = stored_face_scores(result.get('face_scores'))
    try:
        result['model'] = json.loads(result.pop('model_config_snapshot')).get('model')
    except (TypeError, ValueError, AttributeError):
        result['model'] = None
        result.pop('model_config_snapshot', None)
    return result


def stored_face_scores(value):
    try:
        return face_scores(json.loads(value)) if value else None
    except ValueError:
        return None


def invalid_model():
    error(422, 'invalid_model', '请选择有效的生成模型')


def _offered(request: Request, model_id: str | None) -> bool:
    """Whether the image service Settings selects offers this model (V7.11). Only checked for a model the request
    names: the default model always fits the selected service, and a replay keeps the model it recorded."""
    model = get_model(model_id) if model_id else None
    return model is not None and request.app.state.db.get_image_provider() in model.providers


def slot_response(conn, slot, project):
    current_ids = [project['person_a_image_id'], project['person_b_image_id']]
    if project['person_a_role'] == 'groom':
        current_ids.reverse()
    version = conn.execute(
        'SELECT version.id,version.image_asset_id AS asset_id,version.version_number,version.created_at,version.face_scores,version.size_warning,'
        'task.model_config_snapshot,task.reference_image_ids,task.pose_id,task.moment_title,task.moment_shot_size,'
        'task.source_version_id,task.regenerate_mode FROM photo_versions version '
        'JOIN generation_tasks task ON task.id=version.generation_task_id WHERE version.id=?',
        (slot['current_version_id'],),
    ).fetchone() if slot['current_version_id'] else None
    stale = False
    styled_asset_ids = None
    look_ids = None
    room_data = None
    style_art_asset_id = None
    if version is not None:
        try:
            used_references = json.loads(version['reference_image_ids'])
            # A styled photo stands in for the uploaded photo it was made from (V4.0).
            uploaded = [uploaded_asset_id(conn, asset_id) for asset_id in used_references]
            if uploaded[:2] != used_references[:2]:
                styled_asset_ids = {'bride': used_references[0], 'groom': used_references[1]}
            # A face swap records no looks of its own: they are the looks of the picture it started from.
            source = _slot_source_task(conn, slot)
            source_snapshot = json.loads(source['model_config_snapshot']) if source is not None else {}
            looks = source_snapshot.get('looks')
            if isinstance(looks, dict) and looks.get('bride') and looks.get('groom'):
                look_ids = {'bride': looks['bride'], 'groom': looks['groom']}
            # V7.35: the style art the current version was shot from: the last reference, when the task has it.
            slots = source_snapshot.get('reference_slots') or []
            source_references = json.loads(source['reference_image_ids']) if source is not None else []
            if source_snapshot.get('style_art') and slots and len(source_references) == len(slots):
                style_art_asset_id = source_references[-1]
            # V7.34: the room the current version was shot in, and its area.
            room = source_snapshot.get('room')
            if isinstance(room, dict) and (shot_room := get_room(slot['style_id'], room.get('id'))) is not None:
                area_number = room.get('area')
                room_data = {'id': shot_room.id, 'name': shot_room.name, 'area': area_number,
                             'area_name': shot_room.areas[area_number - 1].name if isinstance(area_number, int)
                             and 1 <= area_number <= len(shot_room.areas) else None}
            stale = uploaded[:2] != current_ids
            if not stale:
                side_ids = {project[upload_column(person, facing)] for person in 'ab' for facing in SIDE_FACINGS}
                for asset_id in uploaded[2:]:
                    if asset_id in current_ids:
                        continue
                    asset = conn.execute('SELECT asset_type FROM image_assets WHERE id=?', (asset_id,)).fetchone()
                    if asset is not None and asset['asset_type'] == 'original' and asset_id not in side_ids:
                        stale = True
                        break
        except (TypeError, ValueError, IndexError, AttributeError):
            stale = False
    latest = conn.execute(
        'SELECT task.id,task.status,task.error_code,task.error_message,task.model_config_snapshot,task.regenerate_mode,task.pose_id,task.moment_title,task.moment_shot_size FROM generation_tasks task '
        'WHERE task.target_slot_id=? AND NOT EXISTS ('
        'SELECT 1 FROM generation_tasks retry WHERE retry.retry_of_task_id=task.id) '
        'ORDER BY task.created_at DESC,task.rowid DESC LIMIT 1', (slot['id'],),
    ).fetchone()
    style = STYLE_BY_ID.get(slot['style_id'], {'name': slot['style_id']})
    version_data = None
    if version is not None:
        version_data = version_response({key: version[key] for key in ('id', 'asset_id', 'version_number', 'created_at', 'model_config_snapshot', 'source_version_id', 'regenerate_mode', 'face_scores', 'size_warning')})
    latest_data = {key: latest[key] for key in ('id', 'status', 'error_code', 'error_message', 'model_config_snapshot', 'regenerate_mode')} if latest else None
    if latest_data is not None:
        try:
            latest_data['model'] = json.loads(latest_data.pop('model_config_snapshot')).get('model')
        except (TypeError, ValueError, AttributeError):
            latest_data['model'] = None
            latest_data.pop('model_config_snapshot', None)
    pose_id = None
    shot_size = None
    if slot['kind'] == 'photo':
        pose_id = version['pose_id'] if version is not None else (latest['pose_id'] if latest else None)
        shot_size = version['moment_shot_size'] if version is not None else (latest['moment_shot_size'] if latest else None)
    pose_name = (version['moment_title'] if version is not None and version['moment_title'] else
                 latest['moment_title'] if latest and latest['moment_title'] else
                 POSE_BY_ID.get(pose_id, {}).get('name') if pose_id else None)
    return {
        'id': slot['id'], 'kind': slot['kind'], 'batch_id': slot['batch_id'],
        'style_id': slot['style_id'], 'style_name': style['name'],
        'pose_id': pose_id, 'pose_name': pose_name, 'shot_size': shot_size,
        'current_version': version_data,
        'version_count': conn.execute('SELECT COUNT(*) FROM photo_versions WHERE photo_slot_id=?', (slot['id'],)).fetchone()[0],
        'latest_task': latest_data, 'is_stale': stale, 'styled_asset_ids': styled_asset_ids, 'look_ids': look_ids,
        'room': room_data, 'style_art_asset_id': style_art_asset_id,
    }


def _ordered_photo_slots(conn, project_id: str):
    return conn.execute(
        "SELECT * FROM photo_slots WHERE project_id=? AND kind='photo' ORDER BY created_at,rowid",
        (project_id,),
    ).fetchall()


def batch_response(conn, batch):
    counts = dict.fromkeys(STATUSES, 0)
    for row in conn.execute(
        'SELECT task.status,COUNT(*) AS n FROM generation_tasks task '
        'WHERE task.batch_id=? AND NOT EXISTS ('
        'SELECT 1 FROM generation_tasks retry WHERE retry.retry_of_task_id=task.id) '
        'GROUP BY task.status',
        (batch['id'],),
    ):
        counts[row['status']] = row['n']
    status = 'completed'
    if counts['waiting'] or counts['running']:
        status = 'running'
    elif counts['held']:
        status = 'held'
    elif counts['failed'] or counts['interrupted']:
        status = 'needs_attention'
    style_id = batch['style_id'] if 'style_id' in batch.keys() else None
    return {
        'id': batch['id'], 'project_id': batch['project_id'], 'batch_type': batch['batch_type'],
        'style_id': style_id, 'style_name': STYLE_BY_ID.get(style_id, {}).get('name') if style_id else None,
        'created_at': batch['created_at'], 'status': status, 'total': sum(counts.values()), 'counts': counts,
        'paused': 'paused_at' in batch.keys() and batch['paused_at'] is not None,
    }


def project_response(conn, project, *, detail: bool = True):
    if not project['person_a_image_id'] or not project['person_b_image_id']:
        stage = 'upload'
    else:
        stage = 'shooting' if conn.execute("SELECT 1 FROM photo_slots WHERE project_id=? AND kind='photo' LIMIT 1", (project['id'],)).fetchone() else 'style'
    cover = None
    row = conn.execute("SELECT v.image_asset_id FROM photo_slots s JOIN photo_versions v ON v.id=s.current_version_id WHERE s.project_id=? AND s.kind='photo' ORDER BY s.created_at,s.rowid LIMIT 1", (project['id'],)).fetchone()
    if row is None:
        row = conn.execute("SELECT v.image_asset_id FROM photo_slots s JOIN photo_versions v ON v.id=s.current_version_id WHERE s.project_id=? AND s.kind='preview' ORDER BY s.created_at,s.rowid LIMIT 1", (project['id'],)).fetchone()
    if row:
        cover = row['image_asset_id']
    counts = {row['status']: row['n'] for row in conn.execute(
        'SELECT task.status,COUNT(*) AS n FROM generation_tasks task '
        'WHERE task.project_id=? AND NOT EXISTS ('
        'SELECT 1 FROM generation_tasks retry WHERE retry.retry_of_task_id=task.id) '
        'GROUP BY task.status',
        (project['id'],),
    )}
    result = {key: project[key] for key in ('id', 'name', 'created_at', 'updated_at')}
    result.update(stage=stage, cover_asset_id=cover,
                  photo_count=conn.execute("SELECT COUNT(*) FROM photo_slots WHERE project_id=? AND kind='photo' AND current_version_id IS NOT NULL", (project['id'],)).fetchone()[0],
                  active_task_count=counts.get('waiting', 0) + counts.get('running', 0),
                  has_pending_recovery=bool(counts.get('held', 0) or counts.get('interrupted', 0)))
    if not detail:
        return result
    # V7.56: a style has a sample per room; they come in the styles' order, then the rooms' order (no room first).
    order = {style['id']: index for index, style in enumerate(STYLES)}
    room_order = lambda row: next((index for index, room in enumerate(style_rooms(row['style_id'])) if room.id == row['room_id']), -1)
    rows = conn.execute("SELECT * FROM photo_slots WHERE project_id=? AND kind='preview'", (project['id'],)).fetchall()
    previews = [slot_response(conn, slot, project) for slot in sorted(
        (row for row in rows if row['style_id'] in order), key=lambda row: (order[row['style_id']], room_order(row), row['created_at']))]
    photos = [slot_response(conn, row, project) for row in _ordered_photo_slots(conn, project['id'])]
    photo_sets = []
    for row in conn.execute("SELECT b.style_id, MIN(b.created_at) AS first_created FROM generation_batches b WHERE b.project_id=? AND b.batch_type='photo' GROUP BY b.style_id ORDER BY first_created", (project['id'],)):
        style_id = row['style_id']
        slots_for_style = [p for p in photos if p['style_id'] == style_id]
        batch_ids = [r['id'] for r in conn.execute("SELECT id FROM generation_batches WHERE project_id=? AND batch_type='photo' AND style_id=? ORDER BY created_at,rowid", (project['id'], style_id))]
        photo_sets.append({'style_id': style_id, 'style_name': STYLE_BY_ID.get(style_id, {'name': style_id})['name'], 'slot_count': len(slots_for_style), 'completed_count': sum(p['current_version'] is not None for p in slots_for_style), 'batch_ids': batch_ids})
    # Read pending scoring and its results in this same transaction, so a completed
    # generation cannot stop the client's polling before its score is visible.
    pending_scores = conn.execute(
        'SELECT EXISTS(SELECT 1 FROM photo_versions version '
        'JOIN photo_slots slot ON slot.id=version.photo_slot_id '
        'WHERE slot.project_id=? AND version.face_score_pending=1 '
        'UNION ALL SELECT 1 FROM looks WHERE project_id=? AND face_score_pending=1 '
        'UNION ALL SELECT 1 FROM makeups WHERE project_id=? AND face_score_pending=1)',
        (project['id'], project['id'], project['id']),
    ).fetchone()[0]
    result.update(has_pending_face_scores=bool(pending_scores),
                  makeup_level=project['makeup_level'], groom_makeup_level=project['groom_makeup_level'],
                  bride_retouch_settings=project_retouch_settings(project, 'bride'),
                  groom_retouch_settings=project_retouch_settings(project, 'groom'),
                  bride_hair_length=project['bride_hair_length'], groom_hair_length=project['groom_hair_length'],
                  person_a={'asset_id': project['person_a_image_id'], 'role': project['person_a_role'],
                            **{f'{facing}_asset_id': project[upload_column('a', facing)] for facing in SIDE_FACINGS}},
                  person_b={'asset_id': project['person_b_image_id'], 'role': 'groom' if project['person_a_role'] == 'bride' else 'bride',
                            **{f'{facing}_asset_id': project[upload_column('b', facing)] for facing in SIDE_FACINGS}},
                  previews=previews, photo_sets=photo_sets, photos=photos,
                  looks=[look_response(project, row) for row in conn.execute(
                      "SELECT * FROM looks WHERE project_id=? AND angle='front' AND discarded_at IS NULL ORDER BY created_at,rowid",
                      (project['id'],))],
                  side_looks=current_side_looks(conn, project),
                  style_looks=style_looks(conn, project), style_outfits=style_outfits(conn, project),
                  makeups=[makeup_response(project, row) for row in conn.execute(
                      'SELECT * FROM makeups WHERE project_id=? AND discarded_at IS NULL ORDER BY created_at,rowid', (project['id'],))],
                  default_look_items={role: [{'hairstyle_id': hairstyle} for hairstyle in default_look_items(project, role)]
                                      for role in ('bride', 'groom')})
    return result


def _version_task(conn, version_id):
    return conn.execute(
        'SELECT task.* FROM photo_versions version '
        'JOIN generation_tasks task ON task.id=version.generation_task_id WHERE version.id=?',
        (version_id,),
    ).fetchone()


def _slot_source_task(conn, slot):
    """The generation task behind the slot's current image.

    A face-swap, AI face-fix or hand-fix version has no prompt of its own (it only corrects faces or hands), so
    actions such as identity boost or change shot must start from the generation task the fix was applied to.
    """
    if slot['current_version_id']:
        task = _version_task(conn, slot['current_version_id'])
        while task is not None and task['regenerate_mode'] in FIX_MODES and task['source_version_id']:
            task = _version_task(conn, task['source_version_id'])
        return task
    return conn.execute(
        'SELECT task.* FROM generation_tasks task WHERE task.target_slot_id=? AND NOT EXISTS ('
        'SELECT 1 FROM generation_tasks retry WHERE retry.retry_of_task_id=task.id) '
        'ORDER BY task.created_at DESC,task.rowid DESC LIMIT 1',
        (slot['id'],),
    ).fetchone()


def _slot_user_label(conn, slot, *, position: int | None = None) -> str:
    if slot['kind'] == 'preview':
        style_name = STYLE_BY_ID.get(slot['style_id'], {}).get('name', '该风格')
        return f'「{style_name}」样片'
    if position is None:
        return '这张照片'
    source = _slot_source_task(conn, slot)
    pose_id = source['pose_id'] if source is not None else slot['pose_id']
    pose_name = (source['moment_title'] if source is not None and source['moment_title'] else
                 POSE_BY_ID.get(pose_id, {}).get('name') if pose_id else None)
    return f'第 {position} 张（{pose_name}）' if pose_name else f'第 {position} 张照片'


def _slot_action_label(conn, slot) -> str:
    if slot['kind'] == 'preview':
        return _slot_user_label(conn, slot)
    slots = _ordered_photo_slots(conn, slot['project_id'])
    position = next((index for index, item in enumerate(slots, 1) if item['id'] == slot['id']), None)
    return _slot_user_label(conn, slot, position=position)


def _slot_busy_task(conn, slot_id: str):
    return conn.execute(
        "SELECT task.* FROM generation_tasks task WHERE task.target_slot_id=? "
        "AND task.status IN ('waiting','running','held') AND NOT EXISTS ("
        'SELECT 1 FROM generation_tasks retry WHERE retry.retry_of_task_id=task.id) '
        "ORDER BY CASE task.status WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,task.created_at,task.rowid LIMIT 1",
        (slot_id,),
    ).fetchone()


def _slot_is_busy(conn, slot_id: str) -> bool:
    return _slot_busy_task(conn, slot_id) is not None


def _parse_json_list(value: str, slot_label: str) -> list[str]:
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        error(409, 'slot_not_ready', f'{slot_label}的生成参考记录无效，暂时无法重试')
    if not isinstance(result, list) or any(not isinstance(item, str) for item in result):
        error(409, 'slot_not_ready', f'{slot_label}的生成参考记录无效，暂时无法重试')
    return result


def _parse_snapshot(value: str, slot_label: str) -> dict:
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        error(409, 'slot_not_ready', f'{slot_label}的模型记录无效，暂时无法重试')
    if not isinstance(result, dict):
        error(409, 'slot_not_ready', f'{slot_label}的模型记录无效，暂时无法重试')
    return result


def _build_references(project, bride_facing: str = 'front', groom_facing: str = 'front',
                      preview_asset_id: str | None = None, *, looks=None) -> tuple[list[str], list[tuple[str, str]]]:
    """Reference images in the fixed order [bride front, groom front, side photos..., preview?],
    plus the (role, facing) of each side photo for the prompt's image-role list.

    looks: the (bride, groom) look rows the shot uses (V5.0); their images are the fronts. Side photos stay the
    uploads here and are replaced by side looks when the task runs."""
    if not project['person_a_image_id'] or not project['person_b_image_id']:
        error(409, 'photos_missing', '请先上传两个人的照片')
    person_of = {project['person_a_role']: 'a', ('groom' if project['person_a_role'] == 'bride' else 'bride'): 'b'}
    references = [project[upload_column(person_of['bride'], 'front')], project[upload_column(person_of['groom'], 'front')]]
    if looks is not None:
        references = [looks[0]['image_asset_id'], looks[1]['image_asset_id']]
    sides: list[tuple[str, str]] = []
    for role, facing in (('bride', bride_facing), ('groom', groom_facing)):
        if facing == 'front':
            continue
        # V7.48: the photo of this angle, else the other one of the same direction; none means only the front is sent.
        asset_id, shown = uploaded_side(project, person_of[role], facing)
        if asset_id:
            references.append(asset_id)
            sides.append((role, shown))
    if preview_asset_id:
        references.append(preview_asset_id)
    return references, sides


def _style_preview_reference(conn, project_id: str, style_id: str, slot_label: str, room: str | None = None) -> str:
    """The sample a shot copies (with_preview mode): its style's sample in the shot's room (V7.56: one per room)."""
    style_name = STYLE_BY_ID.get(style_id, {}).get('name', '该风格')
    preview_label = f'「{style_name}」样片'
    preview = conn.execute(
        "SELECT current_version_id FROM photo_slots WHERE project_id=? AND kind='preview' AND style_id=? AND room_id IS ?",
        (project_id, style_id, room),
    ).fetchone()
    if preview is None or preview['current_version_id'] is None:
        error(409, 'slot_not_ready', f'{preview_label}尚未生成成功，无法作为{slot_label}的参考')
    version = conn.execute(
        'SELECT image_asset_id FROM photo_versions WHERE id=?', (preview['current_version_id'],),
    ).fetchone()
    if version is None:
        error(409, 'slot_not_ready', f'{preview_label}尚未生成成功，无法作为{slot_label}的参考')
    return version['image_asset_id']


def _validate_reference_capacity(model_id: str, references: list[str], needed: int | None = None):
    """needed: the references the shot will send when that is more than it has now (a style art made later)."""
    model = get_model(model_id)
    if model is None or (needed if needed is not None else len(references)) > model.max_reference_images:
        invalid_model()


def _fresh_snapshot(request: Request, model_id: str, aspect_ratio: str, looks=None,
                    side_references: list[tuple[str, str]] = (), with_preview: bool = False, outfit: str | None = None,
                    art_style: str | None = None, room: str | None = None, area: int | None = None) -> str:
    """looks: the (bride, groom) look rows of a shot (V5.0), recorded with what each reference image is.
    outfit: the fixed outfit of the shot (V7.17), recorded so its later shots wear the same one.
    art_style: the style whose art is the shot's last reference (V7.33); None without one.
    room, area: the room area the shot is in (V7.34), recorded so that its set and redos stay in the room."""
    snapshot = dict(request.app.state.config.model_config_snapshot)
    snapshot['model'] = model_id
    snapshot['aspect_ratio'] = aspect_ratio
    # V7.11: the image service the request goes to when the task is made (base_url is always 88API's).
    snapshot['provider'] = provider_for(get_model(model_id), request.app.state.db.get_image_provider())
    if looks is not None:
        snapshot['looks'] = look_snapshot(looks[0]['id'], looks[1]['id'])
        # V7.41: the two looks are sent as one picture (the prompt says so: prompts.pair_looks).
        snapshot['paired_looks'] = True
        # V7.43: so are the side looks when the shot has two or more (their prompt says so too).
        snapshot['paired_sides'] = True
        snapshot['reference_slots'] = reference_slots(list(side_references), with_preview, art_style is not None)
    if outfit is not None:
        snapshot['outfit'] = outfit
    if room is not None:
        snapshot['room'] = {'id': room, 'area': area}
    if art_style is not None:
        snapshot['style_art'] = style_art_snapshot(art_style, outfit, room, area)
    return json.dumps(snapshot, ensure_ascii=False)


def _usable_look(conn, project, look_id: str, role: str, label: str):
    """The look row, checked that it is this project's, this role's and still usable for a new shot."""
    look = conn.execute('SELECT * FROM looks WHERE id=? AND project_id=?', (look_id, project['id'])).fetchone()
    if look is None or look['role'] != role:
        error(422, 'invalid_look', f'{label}的{"新娘" if role == "bride" else "新郎"}造型不存在')
    problem = look_problem(project, look)
    if problem is not None:
        error(409, 'look_unusable', f'{label}：{LOOK_PROBLEM_MESSAGES[problem]}')
    return look


def _source_outfit(source, slot_label: str) -> str | None:
    """The fixed outfit of the picture a new shot starts from (V7.17): a set wears its sample's outfit, whatever the
    project has chosen since. None for pictures before outfits, whose shots keep the style's one-line costume."""
    return _parse_snapshot(source['model_config_snapshot'], slot_label).get('outfit')


def _source_looks(conn, project, source, slot_label: str):
    """The looks of the picture a slot action starts from; a new shot keeps them (V5.0)."""
    looks = _parse_snapshot(source['model_config_snapshot'], slot_label).get('looks')
    if not isinstance(looks, dict) or not looks.get('bride') or not looks.get('groom'):
        error(409, 'looks_required', f'{slot_label}来自旧版造型流程，没有记录造型；请先在「风格与造型」里做好并选用这个风格的造型，再重新生成这个风格的样片')
    return (_usable_look(conn, project, looks['bride'], 'bride', slot_label),
            _usable_look(conn, project, looks['groom'], 'groom', slot_label))


def _shot_ratio(request: Request, orientation: str) -> str:
    return ratio_for(orientation, request.app.state.db.get_portrait_ratio())


def _source_aspect_ratio(source, request: Request) -> str:
    try:
        ratio = json.loads(source['model_config_snapshot']).get('aspect_ratio')
    except (TypeError, ValueError, AttributeError):
        ratio = None
    # Tasks created before per-shot frames all used the configured ratio.
    return ratio or request.app.state.config.aspect_ratio


def _director(request: Request):
    director = request.app.state.director
    if request.app.state.config.generator == '88api' and not request.app.state.db.get_director_model():
        error(409, 'director_not_configured', '请先在设置中填写 AI 导演使用的文字模型')
    return director


def _director_call(request: Request, operation, *, invalid_message='AI 导演没有写出符合要求的方案，请重试'):
    try:
        result = operation(_director(request))
        if isinstance(result, list):
            return [item if isinstance(item, Moment) else Moment(item.get('title'), item.get('shot_size'), item.get('text'), item.get('orientation'), item.get('bride_facing'), item.get('groom_facing'), item.get('scene'), item.get('faces_clear', True)) if isinstance(item, dict) else Moment(None, None, None) for item in result]
        return result if isinstance(result, Moment) else Moment(result.get('title'), result.get('shot_size'), result.get('text'), result.get('orientation'), result.get('bride_facing'), result.get('groom_facing'), result.get('scene'), result.get('faces_clear', True)) if isinstance(result, dict) else Moment(None, None, None)
    except DirectorFailed as exc:
        error(502, 'director_failed', exc.reason)
    except DirectorInvalidOutput:
        error(502, 'director_invalid_output', invalid_message)
    except Exception as exc:
        redacted = str(exc).replace(request.app.state.config.api_key, '[REDACTED]')
        logger.error('Director call failed: %s', redacted)
        error(502, 'director_failed', 'AI 导演暂时无法响应，请稍后重试')


def _director_images(request: Request, style_id: str, room: str | None, single_variant: bool) -> list:
    """(label, path) of the room's picture the director may look at (V7.42): its main picture, which shows every area
    of the room since V7.50; the sample is never sent. None without a room."""
    the_room = get_room(style_id, room)
    if the_room is None:
        return []
    if single_variant or len(the_room.areas) == 1:  # V7.53: a backdrop has one area
        return [(f'场景 1「{the_room.areas[0].name}」的实景图：', main_image(style_id, room))]
    names = '、'.join(f'{number}「{area.name}」' for number, area in enumerate(the_room.areas, 1))
    return [(f'房间「{the_room.name}」的实景图，场景 {names} 都在这张图里：', main_image(style_id, room))]


def _project_facings(project) -> dict[str, tuple[str, ...]]:
    """The facings each person has a photo of (V7.48); the director turns clearly visible faces only to these."""
    person_of = {project['person_a_role']: 'a', ('groom' if project['person_a_role'] == 'bride' else 'bride'): 'b'}
    return {role: available_facings(project, person) for role, person in person_of.items()}


def _director_write(request: Request, *, style: str, scene: str, scene_count: int, count: int, shot_requirement: str,
                    avoid_titles: list[str], facings: dict, images: list | None = None) -> list[Moment]:
    extra = {'images': images} if images else {}
    extra['facings'] = facings
    director = _director(request)
    try:
        first = _director_call(request, lambda value: value.write_moments(style, scene, count, shot_requirement, avoid_titles,
                                                                          scene_count=scene_count, **extra))
        problems = validate_moments(first, count=count, avoid_titles=avoid_titles, scene_count=scene_count, allowed_facings=facings)
        if problems:
            feedback = '上面的输出不符合要求：' + '；'.join(problems) + '。请修正后重新输出完整 JSON。'
            second = _director_call(request, lambda value: value.write_moments(style, scene + '\n\n' + feedback, count, shot_requirement,
                                                                               avoid_titles, scene_count=scene_count, **extra))
            problems = validate_moments(second, count=count, avoid_titles=avoid_titles, scene_count=scene_count,
                                        allowed_facings=facings)
            if problems:
                raise DirectorInvalidOutput(problems)
            return second
        return first
    except DirectorInvalidOutput:
        error(502, 'director_invalid_output', 'AI 导演没有写出符合要求的方案，请重试')


def _director_rewrite_shot(request: Request, *, style: str, scene: str, original: str, target: str,
                           orientation: str, facings: dict, images: list | None = None) -> Moment:
    extra = {'images': images} if images else {}
    extra['facings'] = facings
    try:
        first = _director_call(request, lambda value: value.rewrite_shot(style, scene, original, target, orientation, **extra))
        problems = validate_moment(first, target_shot=target, target_orientation=orientation, check_facing_text=True,
                                   allowed_facings=facings)
        if problems:
            feedback = '上面的输出不符合要求：' + '；'.join(problems) + '。请修正后重新输出完整 JSON。'
            second = _director_call(request, lambda value: value.rewrite_shot(style, scene + '\n\n' + feedback, original, target, orientation,
                                                                              **extra))
            problems = validate_moment(second, target_shot=target, target_orientation=orientation, check_facing_text=True,
                                       allowed_facings=facings)
            if problems:
                raise DirectorInvalidOutput(problems)
            return second
        return first
    except DirectorInvalidOutput:
        error(502, 'director_invalid_output', 'AI 导演没有写出符合要求的方案，请重试')


def _draft_response(row, moments):
    return {
        'id': row['id'], 'project_id': row['project_id'], 'preview_slot_id': row['preview_slot_id'],
        'style_id': row['style_id'], 'style_name': STYLE_BY_ID.get(row['style_id'], {}).get('name', row['style_id']),
        'director_model': row['director_model'], 'moments': moments, 'used_by_batch_id': row['used_by_batch_id'],
        'created_at': row['created_at'], 'updated_at': row['updated_at'],
    }


LEGACY_DRAFT_SHOTS = {'medium': 'three_quarter', 'extreme_close': 'close', 'detail': 'close'}


def _load_draft(conn, draft_id: str):
    row = conn.execute('SELECT * FROM moment_drafts WHERE id=?', (draft_id,)).fetchone()
    if row is None:
        error(404, 'draft_not_found', '导演方案不存在')
    try:
        # Legacy drafts may lack orientation and facing; only while loading them, use portrait/front defaults.
        # Drafts from the retired V3.4–V3.11 ladder also move to the closest current framing.
        moments = [Moment(**{'orientation': 'portrait', 'bride_facing': 'front', 'groom_facing': 'front', **item,
                             'shot_size': LEGACY_DRAFT_SHOTS.get(item.get('shot_size'), item.get('shot_size'))})
                   for item in json.loads(row['moments'])]
    except (TypeError, ValueError, KeyError):
        error(409, 'draft_invalid', '导演方案记录无效')
    return row, moments


def _pose_for_slot(conn, slot) -> str | None:
    task = _slot_source_task(conn, slot)
    return task['pose_id'] if task is not None else None


def _choose_pose(conn, project_id: str, style_id: str, slot, already_assigned: set[str]) -> str:
    counts = {pose['id']: 0 for pose in POSES}
    style_slots = conn.execute(
        "SELECT * FROM photo_slots WHERE project_id=? AND kind='photo' AND style_id=? ORDER BY created_at,rowid",
        (project_id, style_id),
    ).fetchall()
    current_pose = _pose_for_slot(conn, slot)
    for existing in style_slots:
        pose_id = _pose_for_slot(conn, existing)
        if pose_id in counts:
            counts[pose_id] += 1
    candidates = [pose['id'] for pose in POSES if pose['id'] != current_pose]
    ranked = sorted(candidates, key=lambda pose_id: (counts[pose_id], int(pose_id[-2:])))
    selected = next((pose_id for pose_id in ranked if pose_id not in already_assigned), ranked[0])
    return selected


def _source_facing(source, slot_label: str) -> dict[str, str]:
    """Which way each person faced in a shot, from the image roles it recorded (V7.30): 'front' or a side facing."""
    facing = {'bride': 'front', 'groom': 'front'}
    for slot in _parse_snapshot(source['model_config_snapshot'], slot_label).get('reference_slots') or []:
        if slot is not None and slot[0] in facing and slot[1] in SIDE_FACINGS:
            facing[slot[0]] = slot[1]
    return facing


def _prepare_slot_action(conn, slot, project, action: str, explicit_model: str | None,
                         request: Request, *, slot_label: str, selected_pose: str | None = None,
                         target_shot: str | None = None, avoid_extra: list[str] | None = None) -> dict:
    slot_id = slot['id']
    if _slot_is_busy(conn, slot_id):
        error(409, 'slot_busy', f'{slot_label}已有任务正在排队或生成，请等它完成后再操作')
    source = _slot_source_task(conn, slot)
    if source is None:
        error(409, 'slot_not_ready', f'{slot_label}尚无生成记录，暂时无法操作')

    if action == 'same':
        references = _parse_json_list(source['reference_image_ids'], slot_label)
        source_snapshot = _parse_snapshot(source['model_config_snapshot'], slot_label)
        effective_model = explicit_model or source_snapshot.get('model') or request.app.state.db.get_default_model()
        if explicit_model is not None and not _offered(request, explicit_model):
            invalid_model()
        _validate_reference_capacity(effective_model, references)
        snapshot = source['model_config_snapshot']
        if explicit_model is not None:
            source_snapshot['model'] = explicit_model
            snapshot = json.dumps(source_snapshot, ensure_ascii=False)
        return {
            'slot_id': slot_id, 'style_id': source['style_id'], 'pose_id': source['pose_id'],
            'moment_title': source['moment_title'], 'moment_shot_size': source['moment_shot_size'],
            'references': source['reference_image_ids'], 'prompt': source['prompt'],
            'snapshot': snapshot, 'regenerate_mode': 'same',
        }

    style_id = source['style_id']
    # Boosting likeness, changing the pose or the shot is a new shot from the styled photos of the project's
    # source picture's looks (V5.0); only 'same' replays the old prompt and reference images as they were.
    looks = _source_looks(conn, project, source, slot_label)
    outfit = _source_outfit(source, slot_label)
    # V7.21: the makeup reminder follows the makeup the bride's look wears, which may be another style's.
    makeup_style = makeup_style_of(looks[0])
    # V7.64: the shot copies the bride look's jewelry, or says she wears none.
    jewelry = looks[0]['jewelry_id'] is not None
    batch_reference_mode = request.app.state.db.get_batch_reference_mode()
    # V7.33: the style art replaces the sample as the reference for the clothes and the place.
    art = style_art_applies(request.app.state.db, style_id, outfit)
    with_preview = slot['kind'] == 'photo' and batch_reference_mode == 'with_preview' and not art
    # V7.34: a new shot stays in its source's room; boosting likeness and changing the shot keep its area too.
    room, area = _source_room(source, slot_label)
    source_scene = area if room is not None else _source_scene(style_id, source['prompt'])
    single_scene = with_preview or (art and room is None)
    references, side_references = _build_references(project, looks=looks)
    if with_preview:
        references.append(_style_preview_reference(conn, project['id'], style_id, slot_label, room))

    aspect_ratio = _source_aspect_ratio(source, request)
    if action == 'identity_boost':
        if source['moment_shot_size'] == DETAIL_SHOT:
            error(409, 'no_faces', f'{slot_label}是局部特写，画面里没有人脸，无需加强长相')
        pose_id = source['pose_id']
        # V7.30: the same shot again, so the same faces turned the same way: the side photos the shot used come back
        # (their side looks), as they did for the shot. Shots recorded before side photos have only fronts.
        facing = _source_facing(source, slot_label)
        if any(value != 'front' for value in facing.values()):
            references, side_references = _build_references(project, facing['bride'], facing['groom'], looks=looks)
            if with_preview:
                references.append(_style_preview_reference(conn, project['id'], style_id, slot_label, room))
        prompt = (compose_identity_boost_moment_prompt(style_id, shot_text_from_prompt(source['prompt']), aspect_ratio,
                                                       scene=source_scene, with_preview=with_preview,
                                                       side_references=side_references,
                                                       styled=True, outfit=outfit, makeup_style=makeup_style, art=art, room=room,
                                                       jewelry=jewelry)
                  if source['moment_shot_size'] else compose_identity_boost_prompt(style_id, pose_id, aspect_ratio, with_preview=with_preview,
                                                                                   styled=True, outfit=outfit, makeup_style=makeup_style,
                                                                                   art=art, room=room, scene=area or 1, jewelry=jewelry))
        regenerate_mode = 'identity_boost'
    elif action == 'change_pose':
        if slot['kind'] != 'photo':
            error(409, 'slot_not_ready', f'{slot_label}不是相册作品，不能更换动作')
        if source['moment_title'] is None and selected_pose is not None:
            pose_id = selected_pose
            prompt = compose_photo_prompt(style_id, pose_id, aspect_ratio, with_preview=with_preview, styled=True, outfit=outfit,
                                          makeup_style=makeup_style, art=art, room=room, scene=area or 1, jewelry=jewelry)
            regenerate_mode = 'change_pose'
            moment_title, moment_shot_size = None, None
        else:
            style = STYLE_BY_ID[style_id]
            scene = director_scene_text(style_id, single_variant=single_scene, outfit=outfit, room=room)
            avoid = [row['moment_title'] for row in conn.execute(
                'SELECT DISTINCT moment_title FROM generation_tasks WHERE project_id=? AND style_id=? AND moment_title IS NOT NULL',
                (project['id'], style_id)) if row['moment_title']]
            avoid.extend(avoid_extra or [])
            moment = _director_write(request, style=style['name'], scene=scene,
                                     images=_director_images(request, style_id, room, single_scene),
                                     scene_count=director_scene_count(style_id, single_variant=single_scene, room=room), count=1,
                                     shot_requirement=_shot_requirement(1), avoid_titles=avoid,
                                     facings=_project_facings(project))[0]
            pose_id = None
            if room is not None:
                area = moment.scene
            # A new shot gets the frame the director chose for it.
            aspect_ratio = _shot_ratio(request, moment.orientation)
            references, side_references = _build_references(project, moment.bride_facing, moment.groom_facing,
                                                               _style_preview_reference(conn, project['id'], style_id, slot_label, room) if with_preview else None,
                                                               looks=looks)
            prompt = compose_moment_prompt(style_id, moment.text, aspect_ratio, scene=moment.scene, faces_clear=moment.faces_clear,
                                           with_preview=with_preview, side_references=side_references, styled=True, outfit=outfit,
                                           makeup_style=makeup_style, art=art, room=room, jewelry=jewelry)
            regenerate_mode = 'change_pose'
            moment_title, moment_shot_size = moment.title, moment.shot_size
    elif action == 'change_shot':
        if slot['kind'] != 'photo':
            error(409, 'slot_not_ready', f'{slot_label}不是相册作品，不能更换景别')
        if target_shot not in SHOT_SIZES:
            error(422, 'invalid_shot_size', '请选择有效的景别')
        if source['moment_shot_size'] == target_shot:
            error(422, 'same_shot_size', '目标景别与当前景别相同')
        style = STYLE_BY_ID[style_id]
        scene = director_scene_text(style_id, single_variant=single_scene, outfit=outfit, room=room)
        action_text = shot_text_from_prompt(source['prompt'])
        moment = _director_rewrite_shot(request, style=style['name'], scene=scene, original=action_text,
                                        images=_director_images(request, style_id, room, single_scene),
                                        target=target_shot, orientation=orientation_of(aspect_ratio),
                                        facings=_project_facings(project))
        pose_id = None
        references, side_references = _build_references(project, moment.bride_facing, moment.groom_facing,
                                                           _style_preview_reference(conn, project['id'], style_id, slot_label, room) if with_preview else None,
                                                           looks=looks)
        # Changing the shot size keeps the scene and whether it is an atmosphere shot.
        prompt = compose_moment_prompt(style_id, moment.text, aspect_ratio, scene=source_scene,
                                       faces_clear=not is_faceless_shot(source['prompt']),
                                       with_preview=with_preview, side_references=side_references, styled=True, outfit=outfit,
                                       makeup_style=makeup_style, art=art, room=room, jewelry=jewelry)
        regenerate_mode = 'change_shot'
        moment_title, moment_shot_size = moment.title, moment.shot_size
    else:
        raise ValueError(f'Unsupported slot action: {action}')

    model_id = explicit_model or request.app.state.db.get_default_model()
    if explicit_model is not None and not _offered(request, explicit_model):
        invalid_model()
    if room is not None and area is None:
        area = 1
    _, needed = (_add_style_art(conn, request, project['id'], style_id, outfit, references, room, area) if art
                 else (False, len(references)))
    _validate_reference_capacity(model_id, references, needed)
    return {
        'slot_id': slot_id, 'style_id': style_id, 'pose_id': pose_id,
        'moment_title': locals().get('moment_title', source['moment_title']),
        'moment_shot_size': locals().get('moment_shot_size', source['moment_shot_size']),
        'references': json.dumps(references, ensure_ascii=False), 'prompt': pair_looks(prompt),
        'snapshot': _fresh_snapshot(request, model_id, aspect_ratio, looks, side_references, with_preview, outfit,
                                    style_id if art else None, room, area),
        'regenerate_mode': regenerate_mode,
    }


def _face_fix_input(conn, slot, project, *, slot_label: str, what: str):
    """The version a face swap or AI face fix starts from and its task: the current version, or, when that is
    itself a fix, the picture it fixed."""
    if slot['current_version_id'] is None:
        error(409, 'slot_not_ready', f'{slot_label}尚无成功版本，暂时无法{what}')
    if _slot_is_busy(conn, slot['id']):
        error(409, 'slot_busy', f'{slot_label}已有任务正在排队或生成，请等它完成后再操作')
    if not project['person_a_image_id'] or not project['person_b_image_id']:
        error(409, 'photos_missing', '请先上传两个人的照片')

    def version_task(version_id):
        return conn.execute(
            'SELECT version.id AS version_id,task.* FROM photo_versions version '
            'JOIN generation_tasks task ON task.id=version.generation_task_id '
            'WHERE version.id=? AND version.photo_slot_id=?',
            (version_id, slot['id']),
        ).fetchone()

    input_task = version_task(slot['current_version_id'])
    if input_task is None:
        error(409, 'slot_not_ready', f'{slot_label}的当前版本记录不存在，暂时无法{what}')
    while input_task['regenerate_mode'] in FACE_FIX_MODES:
        input_task = version_task(input_task['source_version_id']) if input_task['source_version_id'] else None
        if input_task is None:
            error(409, 'slot_not_ready', f'{slot_label}的来源版本不存在，暂时无法{what}')
    if input_task['moment_shot_size'] == DETAIL_SHOT:
        error(409, 'no_faces', f'{slot_label}是局部特写，画面里没有人脸，无需{what}')
    return input_task


def _prepare_face_swap_spec(conn, slot, project, request: Request, *, slot_label: str) -> dict:
    swap_status = request.app.state.face_swapper.status
    if not swap_status['available']:
        error(409, 'face_swap_unavailable', swap_status['reason'])
    input_task = _face_fix_input(conn, slot, project, slot_label=slot_label, what='换脸修正')
    # The swap uses the faces of the shot it fixes: its looks or styled photos (V4.0, V5.0).
    references = task_face_swap_reference_ids(conn, project, _parse_snapshot(input_task['model_config_snapshot'], slot_label),
                                              _parse_json_list(input_task['reference_image_ids'], slot_label))
    snapshot = face_swap_snapshot(request.app.state.face_swapper.status['mode'], FACE_SWAP_WEIGHT)
    return {
        'slot_id': slot['id'],
        'style_id': input_task['style_id'],
        'pose_id': input_task['pose_id'],
        'moment_title': input_task['moment_title'],
        'moment_shot_size': input_task['moment_shot_size'],
        'references': json.dumps(references, ensure_ascii=False),
        'prompt': '',
        'snapshot': snapshot,
        'regenerate_mode': 'face_swap',
        'source_version_id': input_task['version_id'],
    }


def _edit_choice(request: Request, kind: str, explicit_model: str | None) -> tuple[str, str]:
    """(service, model) of a hand fix ('face_fix'), a face fix ('face') or a wash ('wash') (V7.27): the model the
    request names must be one the kind's service offers."""
    provider, model_id = request.app.state.db.get_edit_choice(kind)
    if explicit_model is not None:
        model = get_model(explicit_model)
        if model is None or provider not in model.providers:
            invalid_model()
        model_id = explicit_model
    return provider, model_id


def _prepare_face_fix_spec(conn, slot, project, request: Request, *, slot_label: str, explicit_model: str | None) -> dict:
    """An AI face fix (V7.1) of the slot's picture: likeness from the picture's own looks (V7.4), in its own frame."""
    input_task = _face_fix_input(conn, slot, project, slot_label=slot_label, what='AI 修脸')
    # 2026-11-13: its own service and model (V7.32–2026-11-12: the wash's, while it also washed in 4K).
    provider, model_id = _edit_choice(request, 'face', explicit_model)
    references = _parse_json_list(input_task['reference_image_ids'], slot_label)[:2]
    return {
        'slot_id': slot['id'],
        'style_id': input_task['style_id'],
        'pose_id': input_task['pose_id'],
        'moment_title': input_task['moment_title'],
        'moment_shot_size': input_task['moment_shot_size'],
        'references': json.dumps(references, ensure_ascii=False),
        'prompt': '',
        'snapshot': face_fix_snapshot(model_id, _source_aspect_ratio(input_task, request), provider=provider),
        'regenerate_mode': 'face_fix',
        'source_version_id': input_task['version_id'],
    }


# Edits of the slot's current picture alone (V7.25, V7.26): mode -> (what the user calls it, snapshot builder).
PICTURE_EDITS = {'hand_fix': ('修手', hand_fix_snapshot), 'wash': ('洗图', wash_snapshot)}


def _prepare_picture_edit_spec(conn, slot, project, request: Request, mode: str, *, slot_label: str,
                               explicit_model: str | None) -> dict:
    """A hand fix (V7.25) or wash (V7.26) of the slot's current picture, earlier fixes included: one edit of the
    picture alone."""
    what, snapshot = PICTURE_EDITS[mode]
    if slot['current_version_id'] is None:
        error(409, 'slot_not_ready', f'{slot_label}尚无成功版本，暂时无法{what}')
    if _slot_is_busy(conn, slot['id']):
        error(409, 'slot_busy', f'{slot_label}已有任务正在排队或生成，请等它完成后再操作')
    current = _version_task(conn, slot['current_version_id'])
    shot = _slot_source_task(conn, slot)
    if current is None or shot is None:
        error(409, 'slot_not_ready', f'{slot_label}的当前版本记录不存在，暂时无法{what}')
    source_version_id = slot['current_version_id']
    # V7.83 (the user: 每次点洗图，都不要洗洗图的图): a wash of a washed picture starts again from the picture before
    # the washes, so washes never pile up and each one compares with the unwashed picture.
    while mode == 'wash' and current['regenerate_mode'] == 'wash' and current['source_version_id']:
        source_version_id = current['source_version_id']
        current = _version_task(conn, source_version_id)
        if current is None:
            error(409, 'slot_not_ready', f'{slot_label}洗图之前的版本已不存在，暂时无法{what}')
    # A hand fix uses the Settings 'face_fix' service and model; a wash the wash ones (V7.27).
    provider, model_id = _edit_choice(request, 'wash' if mode == 'wash' else 'face_fix', explicit_model)
    # Only the picture is sent; the two fronts are recorded so the result is scored against the couple.
    references = _parse_json_list(current['reference_image_ids'], slot_label)[:2]
    return {
        'slot_id': slot['id'],
        'style_id': shot['style_id'],
        'pose_id': shot['pose_id'],
        'moment_title': shot['moment_title'],
        'moment_shot_size': shot['moment_shot_size'],
        'references': json.dumps(references, ensure_ascii=False),
        # V7.79: the wash prompt is kept on the task, so a retry repeats the words it was made with.
        'prompt': WASH_REPAIR_PROMPT if mode == 'wash' else '',
        'snapshot': snapshot(model_id, _source_aspect_ratio(shot, request), provider),
        'regenerate_mode': mode,
        'source_version_id': source_version_id,
    }


def _insert_regenerate_batch(conn, project_id: str, client_request_id: str, specs: list[dict]):
    batch_id, now = new_id(), utc_now()
    styles = {spec['style_id'] for spec in specs}
    style_id = next(iter(styles)) if len(styles) == 1 else None
    conn.execute(
        'INSERT INTO generation_batches (id,project_id,batch_type,client_request_id,created_at,style_id) '
        "VALUES (?,?, 'regenerate',?,?,?)",
        (batch_id, project_id, client_request_id, now, style_id),
    )
    for spec in specs:
        conn.execute(
            'INSERT INTO generation_tasks '
            '(id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,pose_id,moment_title,moment_shot_size,prompt,'
            'model_config_snapshot,regenerate_mode,source_version_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (new_id(), project_id, batch_id, spec['slot_id'], 'waiting', spec['references'],
             spec['style_id'], spec.get('pose_id'), spec.get('moment_title'), spec.get('moment_shot_size'), spec['prompt'], spec['snapshot'],
             spec['regenerate_mode'], spec.get('source_version_id'), utc_now()),
        )
    conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, project_id))
    return batch_response(conn, get_batch(conn, batch_id))


@router.get('/health')
def health(request: Request):
    config = request.app.state.config
    return {'ok': True, 'generator': config.generator, 'model': request.app.state.db.get_default_model(),
            'face_swap': request.app.state.face_swapper.status}


@router.get('/models', response_model=list[Model])
def models(request: Request, provider: Literal['88api', 'pptoken', 'ark'] | None = None):
    """The models of an image service: the one named (V7.27, for the face fix's and the wash's own service), else
    the one Settings selects (V7.11)."""
    return [model.__dict__ for model in provider_models(provider or request.app.state.db.get_image_provider())]


@router.get('/regional-edit/models', response_model=list[Model])
def regional_edit_models(request: Request, provider: Literal['88api', 'pptoken', 'ark'] | None = None):
    """Model support, not a claim that a relay has implemented the annotation fields."""
    return [model.__dict__ for model in provider_models(provider or request.app.state.db.get_image_provider())
            if model.id in REGIONAL_EDIT_MODELS]


@router.get('/regional-edit/options')
def regional_edit_options(request: Request, provider: Literal['88api', 'pptoken', 'ark'] | None = None):
    selected = provider or request.app.state.db.get_image_provider()
    native_markup = selected not in UNSUPPORTED_MARKUP_PROVIDERS
    models = regional_edit_models(request, selected)
    # Each method names the models that can do it: the crop-only models (Nano Banana 2.1) only 'crop'.
    every = [model['id'] for model in models]
    whole = [model_id for model_id in every if model_id not in CROP_ONLY_MODELS]
    methods = [
        {'id': 'crop', 'name': '放大局部修（推荐）', 'available': True,
         'description': '把标记区域连同周围放大后单独修，只把标记区域贴回原图；标记以外的部分和原图尺寸都不变。'},
        {'id': 'reference', 'name': '标记参考（兼容）', 'available': True,
         'description': '发送干净原图与带圈的参考副本，根据参考定位修改。'},
        {'id': 'markup', 'name': '原生圈选标记', 'available': native_markup,
         'description': '发送原图与独立透明标记层，需要服务支持 markup。' if native_markup else 'PPTOKEN 当前拒绝 markup 文件字段，请使用标记参考。'},
        {'id': 'mask', 'name': '蒙版涂抹', 'available': True,
         'description': '透明蒙版引导编辑区域；PPTOKEN 实测未准确遵循区域，请仔细检查结果。' if selected == 'pptoken' else '发送原图与透明蒙版，引导修改涂抹区域。'},
    ]
    return {'models': models,
            'methods': [{**method, 'models': every if method['id'] == 'crop' else whole} for method in methods]}


def _provider_problem(request: Request, provider: str) -> str | None:
    """Why Settings cannot switch to this image service; None when it can."""
    config = request.app.state.config
    if provider == 'pptoken' and config.generator != 'mock' and not config.pptoken_api_key.strip():
        return '没有配置 PPTOKEN 的密钥：在 backend/config.local.json 里填写 pptoken_api_key 后重启后端'
    if provider == 'ark' and config.generator != 'mock' and not config.ark_api_key.strip():
        return '没有配置火山方舟的密钥：在 backend/config.local.json 里填写 ark_api_key 后重启后端'
    return None


def _director_provider_problem(request: Request, provider: str) -> str | None:
    config = request.app.state.config
    return None if config.generator == 'mock' else director_key_problem(config, provider)


def _settings_response(request: Request) -> dict:
    db = request.app.state.db
    return {'default_model': db.get_default_model(), 'batch_reference_mode': db.get_batch_reference_mode(),
            'director_model': db.get_director_model(), 'portrait_ratio': db.get_portrait_ratio(),
            'auto_face_fix': db.get_auto_face_fix(),
            **{f'auto_face_fix_{role}': method for role, method in db.get_auto_face_fix_roles().items()},
            **{f'{kind}_model': db.get_stage_model(kind) for kind in STAGE_KINDS},
            **{f'{kind}_provider': db.get_edit_provider(kind) for kind in STAGE_KINDS},
            'image_provider': db.get_image_provider(),
            'image_providers': [{'id': provider, 'name': name, 'available': _provider_problem(request, provider) is None,
                                 'reason': _provider_problem(request, provider)} for provider, name in PROVIDERS.items()],
            'director_provider': db.get_director_provider(),
            'director_providers': [{'id': provider, 'name': name, 'available': _director_provider_problem(request, provider) is None,
                                    'reason': _director_provider_problem(request, provider)}
                                   for provider, name in PROVIDERS.items() if provider in DIRECTOR_PROVIDERS],
            'style_art_mode': db.get_style_art_mode()}


@router.get('/settings', response_model=Settings)
def get_settings(request: Request):
    return _settings_response(request)


@router.patch('/settings', response_model=Settings)
def patch_settings(body: SettingsPatch, request: Request):
    changes = body.model_dump(exclude_unset=True)
    db = request.app.state.db
    provider = changes.get('image_provider')
    if provider is not None:
        problem = _provider_problem(request, provider)
        if problem is not None:
            error(409, 'provider_unavailable', problem)
    model = changes.get('default_model')
    provider_after = provider or db.get_image_provider()
    if model is not None and (get_model(model) is None or provider_after not in get_model(model).providers):
        invalid_model()
    # V7.27: each step (db.STAGE_KINDS) may use its own service; a model must be one that service offers, and the
    # regional edit's one a model that can do regional edits.
    edit_providers = {}
    for kind in STAGE_KINDS:
        key = f'{kind}_provider'
        if changes.get(key) is not None:
            problem = _provider_problem(request, changes[key])
            if problem is not None:
                error(409, 'provider_unavailable', problem)
        edit_providers[kind] = (changes[key] if key in changes else db.get_edit_provider(kind)) or provider_after
        chosen = changes.get(f'{kind}_model')
        if chosen is not None and (get_model(chosen) is None or edit_providers[kind] not in get_model(chosen).providers
                                   or (kind == 'regional' and chosen not in REGIONAL_EDIT_MODELS)):
            invalid_model()
    if provider is not None:
        db.set_image_provider(provider)
        # The default model must be one the service offers: keep it if it does, else the service's first one.
        if model is None and provider not in get_model(db.get_default_model()).providers:
            db.set_default_model(provider_models(provider)[0].id)
    if model is not None:
        db.set_default_model(model)
    for kind in STAGE_KINDS:
        if f'{kind}_provider' in changes:
            db.set_edit_provider(kind, changes[f'{kind}_provider'])
        if f'{kind}_model' in changes:
            db.set_stage_model(kind, changes[f'{kind}_model'])
        elif db.get_stage_model(kind) and edit_providers[kind] not in get_model(db.get_stage_model(kind)).providers:
            # A model the kind's service (its own, or the image service it follows) does not offer goes back to
            # following the default model.
            db.set_stage_model(kind, None)
    mode = changes.get('batch_reference_mode')
    if mode is not None:
        if mode not in BATCH_REFERENCE_MODES:
            error(422, 'invalid_reference_mode', '请选择有效的批量参考方式')
        request.app.state.db.set_batch_reference_mode(mode)
    ratio = changes.get('portrait_ratio')
    if ratio is not None:
        if ratio not in PORTRAIT_RATIOS:
            error(422, 'invalid_portrait_ratio', '请选择有效的竖版比例')
        request.app.state.db.set_portrait_ratio(ratio)
    if changes.get('auto_face_fix') is not None:
        request.app.state.db.set_auto_face_fix(changes['auto_face_fix'])
    if changes.get('auto_face_fix_bride') or changes.get('auto_face_fix_groom'):
        # V7.29: one person at a time; applied after a preset sent in the same request.
        request.app.state.db.set_auto_face_fix_roles(bride=changes.get('auto_face_fix_bride'), groom=changes.get('auto_face_fix_groom'))
    if changes.get('director_provider') is not None:
        problem = _director_provider_problem(request, changes['director_provider'])
        if problem is not None:
            error(409, 'provider_unavailable', problem)
        db.set_director_provider(changes['director_provider'])
        if hasattr(request.app.state.director, 'provider'):
            request.app.state.director.provider = changes['director_provider']
            request.app.state.director.model = db.get_director_model()
    if 'director_model' in changes:
        director_model = changes['director_model']
        if director_model == '':
            error(422, 'invalid_director_model', 'AI 导演模型不能为空字符串')
        request.app.state.db.set_director_model(director_model)
        if hasattr(request.app.state.director, 'model'):
            request.app.state.director.model = director_model
    if changes.get('style_art_mode') is not None:
        db.set_style_art_mode(changes['style_art_mode'])
    return _settings_response(request)


def _room_image_url(style_id: str, room_id: str) -> str:
    return f'/api/styles/{style_id}/rooms/{room_id}/areas/1/image'


def _rooms_response(conn, config, style_id: str) -> list[dict]:
    return [{'id': room.id, 'name': room.name,
             # V7.50: only the main picture (area 1) has one; it shows the whole room.
             'areas': [{'name': area.name, 'image_url': _room_image_url(style_id, room.id) if number == 1 else None}
                       for number, area in enumerate(room.areas, 1)]}
            for room in style_rooms(style_id)]


@router.get('/styles', response_model=list[Style])
def styles(request: Request):
    with request.app.state.db.connect() as conn:
        return [{**style, 'default_preview': style['id'] in DEFAULT_PREVIEW_STYLE_IDS, 'retired': style['id'] in RETIRED_STYLE_IDS,
                 'rooms': _rooms_response(conn, request.app.state.config, style['id'])}
                for style in STYLES]


@router.get('/styles/{style_id}/rooms/{room_id}/areas/{number}/image')
def get_room_image(style_id: str, room_id: str, number: int, request: Request):
    """A room's main picture (area 1), kept in the repository. V7.50: the other areas have no picture of their own."""
    if get_room(style_id, room_id) is None or number != 1:
        error(404, 'asset_not_found', '图片不存在')
    return FileResponse(main_image(style_id, room_id))


STYLE_ART_FILES = {'product': 'product_path', 'scene': 'scene_path', 'image': 'image_path'}


def _style_art_response(conn, config, style_id: str, room: str | None = None, area: int | None = None,
                        outfit_id: str | None = None) -> dict:
    """One art of the style art page: the style's main-scene art, or (V7.34) the art of one of its rooms (V7.50: of
    the room's main picture, area 1). outfit_id: the outfit it dresses (V7.35: a sample's own); the style's first
    outfit when None."""
    outfit_id = outfit_id or default_outfit(style_id)
    result = {'style_id': style_id, 'style_name': STYLE_BY_ID[style_id]['name'], 'outfit_id': outfit_id,
              'outfit_name': OUTFIT_BY_ID[outfit_id].name}
    if room is not None:
        the_room = get_room(style_id, room)
        area = 1
        result.update(room_id=room, room_name=the_room.name, area=area, area_name=the_room.areas[0].name,
                      area_image_url=_room_image_url(style_id, room))
    latest = latest_style_art(conn, style_id, outfit_id, room, area)
    current = current_style_art(conn, style_id, outfit_id, room, area)
    if latest is not None:
        result.update({key: latest[key] for key in ('id', 'status', 'model', 'error_message', 'created_at', 'finished_at')})
    result['in_use_id'] = current['id'] if current is not None else None
    # Paid edits that making it now costs: a room area keeps the outfit picture its outfit's other rooms share, so
    # only the combination is paid, plus the outfit picture when there is none yet (V7.109: one taken from the
    # repository is turned gray first, also one edit); a main-scene art redoes all three.
    has_product = shared_product(conn, config, outfit_id) is not None
    result['cost'] = (1 + (0 if has_product else 1)) if room is not None else 3
    # The pictures shown: the art shots use; while there is none, whatever the latest attempt made.
    shown = current or latest
    if shown is not None:
        for kind, column in STYLE_ART_FILES.items():
            if shown[column]:
                result[f'{kind}_url'] = f"/api/style-arts/{shown['id']}/files/{kind}"
    return result


@router.get('/style-arts', response_model=list[StyleArt])
def list_style_arts(request: Request):
    with request.app.state.db.connect() as conn:
        result = []
        for style in STYLES:
            if not has_style_art(style['id']):
                continue
            if has_rooms(style['id']):
                result.extend(_style_art_response(conn, request.app.state.config, style['id'], room.id, 1)
                              for room in style_rooms(style['id']))
            else:
                result.append(_style_art_response(conn, request.app.state.config, style['id']))
        return result


@router.get('/slots/{slot_id}/style-arts', response_model=list[StyleArt])
def slot_style_arts(slot_id: str, request: Request):
    """The arts a set from this sample uses (V7.35): its room's in the sample's outfit (one per room since V7.50), or
    the style's main-scene art. Empty when the sample's shots are not made from the style art."""
    with request.app.state.db.connect() as conn:
        slot = get_slot(conn, slot_id)
        if slot['kind'] != 'preview':
            error(409, 'slot_not_preview', '只有样片有这一组的服饰场景参考图')
        source = _slot_source_task(conn, slot)
        if source is None:
            return []
        label = f'「{STYLE_BY_ID.get(slot["style_id"], {"name": slot["style_id"]})["name"]}」样片'
        outfit = _source_outfit(source, label)
        style_id = slot['style_id']
        if not style_art_applies(request.app.state.db, style_id, outfit):
            return []
        config = request.app.state.config
        room, _area = _source_room(source, label)
        if room is None:
            return [_style_art_response(conn, config, style_id, outfit_id=outfit)]
        return [_style_art_response(conn, config, style_id, room, 1, outfit)]


@router.post('/style-arts/{style_id}/generate', response_model=StyleArt, status_code=202)
def generate_style_art(style_id: str, request: Request, room: str | None = None, area: int | None = None,
                       outfit: str | None = None):
    """Make a style's art (again) (V7.33), with the default image model; the art in use stays until the new one is
    ready. The cost is the StyleArt's `cost`: three edits; one for a room (V7.34; area is optional and ignored since
    V7.50), which keeps the shared outfit picture. outfit: one of the style's outfits (V7.35); its first when None."""
    if not has_style_art(style_id):
        error(404, 'style_not_found', '风格不存在或已停用')
    if outfit is not None and outfit not in STYLE_OUTFITS.get(style_id, ()):
        error(422, 'invalid_outfit', '这个风格没有这套服饰')
    outfit = outfit or default_outfit(style_id)
    if has_rooms(style_id):
        the_room = get_room(style_id, room)
        if the_room is None or (area is not None and not 1 <= area <= len(the_room.areas)):
            error(422, 'invalid_room', '请选择这个风格的场景')
        area = 1
    elif room is not None or area is not None:
        error(422, 'invalid_room', '这个风格没有可选的场景')
    db = request.app.state.db
    with db.connect() as conn:
        latest = latest_style_art(conn, style_id, outfit, room, area)
        if latest is not None and latest['status'] == 'running':
            error(409, 'style_art_busy', f'「{STYLE_BY_ID[style_id]["name"]}」的这张服饰场景图正在生成，请等它完成')
    request.app.state.task_manager.request_style_art(style_id, outfit, db.get_edit_choice('style_art')[1], room, area)
    with db.connect() as conn:
        return _style_art_response(conn, request.app.state.config, style_id, room, area, outfit)


@router.get('/style-arts/{art_id}/files/{kind}')
def get_style_art_file(art_id: str, kind: str, request: Request):
    if kind not in STYLE_ART_FILES:
        error(404, 'asset_not_found', '图片不存在')
    with request.app.state.db.connect() as conn:
        art = conn.execute('SELECT * FROM style_arts WHERE id=?', (art_id,)).fetchone()
    if art is None or not art[STYLE_ART_FILES[kind]]:
        error(404, 'asset_not_found', '图片不存在')
    path = asset_path(request.app.state.config, art[STYLE_ART_FILES[kind]])
    if not path.is_file():
        error(404, 'asset_missing', '图片文件缺失，请重新生成')
    return FileResponse(path)


@router.get('/look-options', response_model=LookOptions)
def look_options():
    return {
        'hairstyles': [{'id': item.id, 'name': item.name, 'role': item.role, 'lengths': list(item.lengths), 'updo': item.updo}
                       for item in HAIRSTYLES],
        'makeup_colors': [item._asdict() for item in MAKEUP_COLORS],
        'styles': {style_id: {'primary': primary, 'alternates': list(alternates), 'needs_updo': style_id in UPDO_STYLES}
                   for style_id, (primary, alternates) in STYLE_MAKEUP_COLORS.items()},
        'outfits': [item._asdict() for item in OUTFITS],
        'style_outfits': {style_id: list(outfits) for style_id, outfits in STYLE_OUTFITS.items()},
        'jewelry': [item._asdict() for item in JEWELRY],
        'style_jewelry': dict(STYLE_JEWELRY_SUGGESTION),
    }


def _get_look(conn, look_id: str):
    look = conn.execute("SELECT * FROM looks WHERE id=? AND angle='front' AND discarded_at IS NULL", (look_id,)).fetchone()
    if look is None:
        error(404, 'look_not_found', '造型不存在')
    return look


@router.post('/projects/{project_id}/looks', response_model=list[Look], status_code=201)
def create_looks(project_id: str, body: LookCreate, request: Request, response: Response):
    """Look candidates for one person (V5.0): each item is one paid edit of their uploaded photo. A bride look is
    made for one style, as a hair edit of the style's selected makeup (V7.8); the groom's looks serve every style
    (V7.0)."""
    with request.app.state.db.connect(write=True) as conn:
        project = get_project(conn, project_id)
        existing = conn.execute('SELECT * FROM looks WHERE client_request_id=? ORDER BY rowid', (body.client_request_id,)).fetchall()
        if existing:
            if any(row['project_id'] != project_id or row['person'] != body.person or row['style_id'] != body.style_id
                   for row in existing):
                error(409, 'request_id_reused', '这个请求编号已经用于另一组造型，请刷新后重试')
            response.status_code = 200
            return [look_response(project, row) for row in existing]
        source = project[f'person_{body.person}_image_id']
        if not source:
            error(409, 'photos_missing', '请先上传这个人的照片')
        role = role_of(project, body.person)
        style_id = body.style_id
        if role == 'groom' and style_id is not None:
            error(422, 'invalid_style', '新郎的造型所有风格共用，不需要指定风格')
        if role == 'bride':
            _check_bride_style(conn, project_id, style_id)
        length, level = hair_length_of(project, role), makeup_level_of(project, role)
        for item in body.items:
            hairstyle = HAIRSTYLE_BY_KEY.get((role, item.hairstyle_id))
            if hairstyle is None or length not in hairstyle.lengths:
                error(422, 'invalid_hairstyle', '这个发型不适合当前的头发长度，请换一个')
            if style_id is not None and not fits_style(role, item.hairstyle_id, length, style_id):
                error(422, 'invalid_hairstyle', f'「{STYLE_BY_ID[style_id]["name"]}」需要盘发，请换一个盘发发型')
            if item.jewelry_id is not None and (role != 'bride' or item.jewelry_id not in JEWELRY_BY_ID):
                error(422, 'invalid_jewelry', '没有这种首饰' if role == 'bride' else '新郎的造型不戴首饰')
        # V7.8: every bride look of a style is a hair edit of the style's selected makeup; V7.22: every groom look is
        # a hair edit of his selected retouched photo.
        makeup = selected_makeup(conn, project, role, style_id)
        if makeup is None:
            error(409, 'makeup_required', f'请先为「{STYLE_BY_ID[style_id]["name"]}」生成并选用一张妆容，再做发型' if role == 'bride'
                  else '请先为新郎生成并选用一张修脸照，再做发型')
        color = makeup['makeup_color']
        level = makeup['makeup_level']
        model_id = request.app.state.db.get_edit_choice('look')[1]
        now, ids = utc_now(), []
        for item in body.items:
            look_id = new_id()
            # V7.5: a bride look is two edits, makeup then hair; since V7.8 the makeup is the selected candidate's,
            # so the look only pays for the hair edit. Its prompt records the makeup prompt. V7.22: the groom's too.
            # V7.61: the bride's earrings and necklace, if she picked any.
            hair_prompt = (compose_bride_hair_prompt(item.hairstyle_id, jewelry_id=item.jewelry_id) if role == 'bride'
                           else compose_groom_hair_prompt(item.hairstyle_id))
            prompt = makeup['prompt']
            conn.execute(
                'INSERT INTO looks (id,project_id,person,role,style_id,angle,hairstyle_id,hair_length,makeup_color,makeup_level,'
                'source_asset_id,styling_version,status,model,prompt,hair_prompt,client_request_id,created_at,'
                "makeup_id,makeup_asset_id,makeup_face_score,makeup_style_id,jewelry_id,retouch_settings) VALUES (?,?,?,?,?,'front',?,?,?,?,?,?,'waiting',?,?,?,?,?,?,?,?,?,?,?)",
                (look_id, project_id, body.person, role, style_id, item.hairstyle_id, length, color, level, source,
                 STYLING_VERSION, model_id, prompt, hair_prompt, body.client_request_id, now,
                 makeup['id'], makeup['image_asset_id'], makeup['face_score'], makeup['makeup_style_id'], item.jewelry_id,
                 makeup['retouch_settings']))
            ids.append(look_id)
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, project_id))
        result = [look_response(project, _get_look(conn, look_id)) for look_id in ids]
    request.app.state.task_manager.notify()
    return result


@router.post('/looks/{look_id}/retry', response_model=Look)
def retry_look(look_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        look = _get_look(conn, look_id)
        if look['status'] not in ('failed', 'interrupted'):
            error(409, 'look_not_retryable', '只有失败或中断的造型可以重试')
        project = get_project(conn, look['project_id'])
        if project[f"person_{look['person']}_image_id"] != look['source_asset_id']:
            error(409, 'look_unusable', LOOK_PROBLEM_MESSAGES['photo_replaced'])
        if look['styling_version'] != STYLING_VERSION:
            error(409, 'look_unusable', LOOK_PROBLEM_MESSAGES['outdated'])
        conn.execute("UPDATE looks SET status='waiting',error_code=NULL,error_message=NULL,finished_at=NULL WHERE id=?", (look_id,))
        result = look_response(project, _get_look(conn, look_id))
    request.app.state.task_manager.notify()
    return result


@router.post('/looks/{look_id}/select', response_model=Look)
def select_look(look_id: str, request: Request):
    """Use this look: for its style (a bride look) or for every style (the groom's), in place of the one before."""
    with request.app.state.db.connect(write=True) as conn:
        look = _get_look(conn, look_id)
        project = get_project(conn, look['project_id'])
        problem = look_problem(project, look)
        if problem is not None:
            error(409, 'look_unusable', LOOK_PROBLEM_MESSAGES[problem])
        select_only(conn, look)
        return look_response(project, _get_look(conn, look_id))


@router.post('/looks/{look_id}/unselect', response_model=Look)
def unselect_look(look_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        look = _get_look(conn, look_id)
        conn.execute('UPDATE looks SET selected_order=NULL WHERE id=?', (look_id,))
        return look_response(get_project(conn, look['project_id']), _get_look(conn, look_id))


@router.delete('/looks/{look_id}', status_code=204)
def delete_look(look_id: str, request: Request):
    """Hide a candidate; pictures already shot from it keep its image."""
    with request.app.state.db.connect(write=True) as conn:
        look = _get_look(conn, look_id)
        if look['status'] in ('waiting', 'running'):
            error(409, 'look_busy', '这套造型还在生成，请等它完成后再删除')
        conn.execute('UPDATE looks SET discarded_at=?,selected_order=NULL WHERE id=?', (utc_now(), look_id))
    return Response(status_code=204)


@router.post('/looks/{look_id}/side-looks', response_model=list[SideLook], status_code=201)
def create_side_looks(look_id: str, request: Request):
    """Make the side looks of a front look in the look step (V7.30): one edit per uploaded side photo of that person
    that has no side look yet, queued like a look candidate. Returns the look's side looks."""
    with request.app.state.db.connect(write=True) as conn:
        look = _get_look(conn, look_id)
        project = get_project(conn, look['project_id'])
        if look['status'] != 'ready' or look_problem(project, look) is not None:
            error(409, 'look_unusable', '这套造型还不能用，先选一套生成好的正面造型再做侧面')
        uploads = {facing: project[upload_column(look['person'], facing)] for facing in SIDE_FACINGS}
        if not any(uploads.values()):
            error(409, 'side_photos_missing', '这个人还没有上传侧脸照，请先在上传照片页上传')
        now = utc_now()
        model_id = request.app.state.db.get_edit_choice('look')[1]
        for facing, upload in uploads.items():
            if not upload:
                continue
            angle = direction(facing)  # the row keeps the direction; its side photo tells the half and full side apart
            existing = latest_side_look(conn, look['id'], angle, upload)
            if existing is not None and existing['status'] in ('waiting', 'running', 'ready'):
                continue  # made or on its way; delete it first to make it again
            if existing is not None:
                conn.execute('UPDATE looks SET discarded_at=? WHERE id=?', (now, existing['id']))  # a failed one
            prompt = compose_look_prompt(look['role'], look['hairstyle_id'], look['makeup_level'],
                                         style_id=makeup_style_of(look), facing=facing, jewelry_id=look['jewelry_id'],
                                         retouch_settings=candidate_retouch_settings(look) if look['retouch_settings'] is not None else None,
                                         model_id=model_id)
            conn.execute(
                'INSERT INTO looks (id,project_id,person,role,style_id,angle,parent_look_id,hairstyle_id,hair_length,makeup_color,'
                'makeup_level,source_asset_id,styling_version,status,model,prompt,created_at,makeup_style_id,retouch_settings) '
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'waiting',?,?,?,?,?)",
                (new_id(), project['id'], look['person'], look['role'], look['style_id'], angle, look['id'], look['hairstyle_id'],
                 look['hair_length'], look['makeup_color'], look['makeup_level'], upload, look['styling_version'],
                 model_id, prompt, now, look['makeup_style_id'], look['retouch_settings']))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, project['id']))
        result = [side for side in current_side_looks(conn, project) if side['parent_look_id'] == look['id']]
    request.app.state.task_manager.notify()
    return result


@router.delete('/side-looks/{look_id}', status_code=204)
def delete_side_look(look_id: str, request: Request):
    """Hide a side look (V7.24); the next new shot that needs this angle edits a new one unless the user makes it
    again first (V7.30). Pictures already shot from it, and their replays, keep its image."""
    with request.app.state.db.connect(write=True) as conn:
        side = conn.execute("SELECT * FROM looks WHERE id=? AND angle<>'front' AND discarded_at IS NULL", (look_id,)).fetchone()
        if side is None:
            error(404, 'look_not_found', '侧面造型不存在')
        if side['status'] in ('waiting', 'running'):
            error(409, 'look_busy', '这张侧面造型还在生成，请等它完成后再删除')
        discard_side_look(conn, side, utc_now())
    return Response(status_code=204)


def _check_bride_style(conn, project_id: str, style_id: str | None):
    if style_id not in STYLE_BY_ID:
        error(422, 'invalid_style', '请为新娘的造型选择一个有效的风格')
    # A retired style may only refresh a sample this project already has (as create_previews).
    if style_id in RETIRED_STYLE_IDS and conn.execute(
            "SELECT 1 FROM photo_slots WHERE project_id=? AND kind='preview' AND style_id=?", (project_id, style_id)).fetchone() is None:
        error(422, 'invalid_style', f'「{STYLE_BY_ID[style_id]["name"]}」已停用，不能再做这个风格的造型')


def _get_makeup(conn, makeup_id: str):
    makeup = conn.execute('SELECT * FROM makeups WHERE id=? AND discarded_at IS NULL', (makeup_id,)).fetchone()
    if makeup is None:
        error(404, 'makeup_not_found', '妆容不存在')
    return makeup


@router.post('/projects/{project_id}/makeups', response_model=list[Makeup], status_code=201)
def create_makeups(project_id: str, body: MakeupCreate, request: Request, response: Response):
    """Makeup candidates of the bride for one style (V7.8): each is one paid edit of her uploaded photo, hair
    unchanged. The most similar one is selected unless the user picks one, and new bride looks of the style are
    hair edits of the selected one. V7.22: the groom's retouched photos (no style; one serves every style) work the
    same way for his looks."""
    with request.app.state.db.connect(write=True) as conn:
        project = get_project(conn, project_id)
        existing = conn.execute('SELECT * FROM makeups WHERE client_request_id=? ORDER BY rowid', (body.client_request_id,)).fetchall()
        if existing:
            if any(row['project_id'] != project_id or row['person'] != body.person or row['style_id'] != body.style_id
                   for row in existing):
                error(409, 'request_id_reused', '这个请求编号已经用于另一组妆容，请刷新后重试')
            response.status_code = 200
            return [makeup_response(project, row) for row in existing]
        role = role_of(project, body.person)
        source = project[f'person_{body.person}_image_id']
        if not source:
            error(409, 'photos_missing', '请先上传这个人的照片')
        level = makeup_level_of(project, role)
        retouch_settings = project_retouch_settings(project, role)
        model_id = request.app.state.db.get_edit_choice('makeup')[1]
        if role == 'bride':
            _check_bride_style(conn, project_id, body.style_id)
            prompt = compose_bride_makeup_prompt(level, body.style_id, retouch_settings, model_id=model_id)
            color = bride_look_color(body.style_id)
        else:
            # V7.22: the groom's retouched photo serves every style, like his looks.
            if body.style_id is not None:
                error(422, 'invalid_style', '新郎的修脸照所有风格共用，不需要指定风格')
            prompt, color = compose_groom_retouch_prompt(level, retouch_settings, model_id=model_id), None
        now, ids = utc_now(), [new_id() for _ in range(body.count)]
        for makeup_id in ids:
            conn.execute(
                'INSERT INTO makeups (id,project_id,person,role,style_id,makeup_color,makeup_level,source_asset_id,styling_version,'
                "status,model,prompt,client_request_id,created_at,retouch_settings) VALUES (?,?,?,?,?,?,?,?,?,'waiting',?,?,?,?,?)",
                (makeup_id, project_id, body.person, role, body.style_id, color, level, source, STYLING_VERSION, model_id, prompt,
                 body.client_request_id, now, retouch_settings.model_dump_json()))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, project_id))
        result = [makeup_response(project, _get_makeup(conn, makeup_id)) for makeup_id in ids]
    request.app.state.task_manager.notify()
    return result


@router.post('/makeups/{makeup_id}/retry', response_model=Makeup)
def retry_makeup(makeup_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        makeup = _get_makeup(conn, makeup_id)
        if makeup['status'] not in ('failed', 'interrupted'):
            error(409, 'makeup_not_retryable', '只有失败或中断的妆容可以重试')
        project = get_project(conn, makeup['project_id'])
        if project[f"person_{makeup['person']}_image_id"] != makeup['source_asset_id']:
            error(409, 'makeup_unusable', MAKEUP_PROBLEM_MESSAGES['photo_replaced'])
        if makeup['styling_version'] != STYLING_VERSION:
            error(409, 'makeup_unusable', MAKEUP_PROBLEM_MESSAGES['outdated'])
        conn.execute("UPDATE makeups SET status='waiting',error_code=NULL,error_message=NULL,finished_at=NULL WHERE id=?", (makeup_id,))
        result = makeup_response(project, _get_makeup(conn, makeup_id))
    request.app.state.task_manager.notify()
    return result


@router.post('/makeups/{makeup_id}/select', response_model=Makeup)
def select_makeup_route(makeup_id: str, request: Request):
    """Make new bride looks of this style on this makeup; looks already made keep the makeup they were made on."""
    with request.app.state.db.connect(write=True) as conn:
        makeup = _get_makeup(conn, makeup_id)
        project = get_project(conn, makeup['project_id'])
        problem = makeup_problem(project, makeup)
        if problem is not None:
            error(409, 'makeup_unusable', MAKEUP_PROBLEM_MESSAGES[problem])
        select_makeup(conn, makeup)
        return makeup_response(project, _get_makeup(conn, makeup_id))


@router.post('/makeups/{makeup_id}/reuse', response_model=Makeup)
def reuse_makeup_route(makeup_id: str, body: MakeupReuse, request: Request):
    """Use this makeup for another style too (V7.21): no new edit; the looks made on it come along, except the
    hairstyles the style cannot use. It becomes the style's selected makeup."""
    with request.app.state.db.connect(write=True) as conn:
        makeup = _get_makeup(conn, makeup_id)
        project = get_project(conn, makeup['project_id'])
        if makeup['role'] != 'bride':
            error(422, 'invalid_person', '新郎的修脸照所有风格共用，不需要复用')
        problem = makeup_problem(project, makeup)
        if problem is not None:
            error(409, 'makeup_unusable', MAKEUP_PROBLEM_MESSAGES[problem])
        _check_bride_style(conn, project['id'], body.style_id)
        target_id = reuse_makeup(conn, project, makeup, body.style_id)
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), project['id']))
        return makeup_response(project, _get_makeup(conn, target_id))


@router.delete('/makeups/{makeup_id}', status_code=204)
def delete_makeup(makeup_id: str, request: Request):
    """Hide a candidate; looks already made on it keep its image. Deleting the selected one selects the most
    similar of the rest."""
    with request.app.state.db.connect(write=True) as conn:
        makeup = _get_makeup(conn, makeup_id)
        if makeup['status'] in ('waiting', 'running'):
            error(409, 'makeup_busy', '这张妆容还在生成，请等它完成后再删除')
        conn.execute('UPDATE makeups SET discarded_at=?,selected_by=NULL WHERE id=?', (utc_now(), makeup_id))
        auto_select_makeup(conn, get_project(conn, makeup['project_id']), makeup)
    return Response(status_code=204)


@router.post('/projects', response_model=ProjectDetail, status_code=201)
def create_project(request: Request, body: ProjectCreate = Body(default=ProjectCreate())):
    project_id, now = new_id(), utc_now()
    with request.app.state.db.connect(write=True) as conn:
        conn.execute('INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)', (project_id, body.name, now, now))
        return project_response(conn, get_project(conn, project_id))


@router.get('/projects', response_model=list[ProjectSummary])
def list_projects(request: Request):
    with request.app.state.db.connect() as conn:
        return [project_response(conn, row, detail=False) for row in conn.execute('SELECT * FROM projects ORDER BY updated_at DESC,rowid DESC').fetchall()]


@router.get('/projects/{project_id}', response_model=ProjectDetail)
def project_detail(project_id: str, request: Request):
    with request.app.state.db.connect() as conn:
        return project_response(conn, get_project(conn, project_id))


@router.patch('/projects/{project_id}', response_model=ProjectDetail)
def patch_project(project_id: str, body: ProjectPatch, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        get_project(conn, project_id)
        changes = body.model_dump(exclude_unset=True)
        if changes:
            for role, level_field in (('bride', 'makeup_level'), ('groom', 'groom_makeup_level')):
                settings_field = f'{role}_retouch_settings'
                if settings_field in changes:
                    changes[settings_field] = json.dumps(changes[settings_field], separators=(',', ':'))
                elif level_field in changes:
                    # Selecting a quick preset resets its overrides; unrelated project edits do not.
                    changes[settings_field] = None
            changes['updated_at'] = utc_now()
            conn.execute('UPDATE projects SET ' + ','.join(f'{key}=?' for key in changes) + ' WHERE id=?', (*changes.values(), project_id))
        return project_response(conn, get_project(conn, project_id))


@router.put('/projects/{project_id}/style-outfits/{style_id}', response_model=ProjectDetail)
def choose_style_outfit(project_id: str, style_id: str, body: OutfitChoice, request: Request):
    """The outfit new samples of this style wear (V7.17). Existing samples and their sets keep theirs."""
    with request.app.state.db.connect(write=True) as conn:
        project = get_project(conn, project_id)
        if body.outfit_id not in STYLE_OUTFITS.get(style_id, ()):
            error(422, 'invalid_outfit', '这个风格没有这套服饰')
        conn.execute('INSERT INTO project_outfits (project_id,style_id,outfit_id) VALUES (?,?,?) '
                     'ON CONFLICT(project_id,style_id) DO UPDATE SET outfit_id=excluded.outfit_id', (project_id, style_id, body.outfit_id))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), project_id))
        return project_response(conn, get_project(conn, project_id))


@router.delete('/projects/{project_id}', status_code=204)
def delete_project(project_id: str, request: Request):
    db, config = request.app.state.db, request.app.state.config
    folder = None
    with db.connect(write=True) as conn:
        get_project(conn, project_id)
        conn.execute("UPDATE generation_tasks SET status='cancelled',finished_at=? WHERE project_id=? AND status IN ('waiting','held')", (utc_now(), project_id))
        # Queued look candidates have not been sent yet; one being edited is waited for like a running shot.
        for table in ('looks', 'makeups'):
            conn.execute(f"UPDATE {table} SET status='failed',error_code='cancelled',error_message='项目已删除',finished_at=? "
                         "WHERE project_id=? AND status='waiting'", (utc_now(), project_id))
        busy = (conn.execute("SELECT 1 FROM generation_tasks WHERE project_id=? AND status='running' LIMIT 1", (project_id,)).fetchone() is not None
                or any(conn.execute(f"SELECT 1 FROM {table} WHERE project_id=? AND status='running' LIMIT 1", (project_id,)).fetchone() is not None
                       for table in ('looks', 'makeups')))
        if not busy:
            # Break cyclic references before cascading removal of project-owned rows.
            conn.execute('UPDATE projects SET ' + ','.join(f'{upload_column(person, facing)}=NULL' for person in 'ab' for facing in FACINGS) +
                         ',selected_preview_version_id=NULL WHERE id=?', (project_id,))
            conn.execute('UPDATE photo_slots SET current_version_id=NULL WHERE project_id=?', (project_id,))
            conn.execute('UPDATE generation_batches SET reference_preview_version_id=NULL WHERE project_id=?', (project_id,))
            conn.execute('UPDATE generation_tasks SET output_asset_id=NULL,retry_of_task_id=NULL WHERE project_id=?', (project_id,))
            conn.execute('DELETE FROM photo_versions WHERE photo_slot_id IN (SELECT id FROM photo_slots WHERE project_id=?)', (project_id,))
            folder = config.projects_dir / project_id
            conn.execute('DELETE FROM projects WHERE id=?', (project_id,))
    if busy:
        error(409, 'project_busy', '项目仍有正在生成的任务，已取消剩余任务，请等待生成结束后删除')
    if folder is not None:
        try:
            if folder.exists():
                shutil.rmtree(folder)
        except Exception:
            logger.exception('Project folder removal failed for %s', project_id)
    return Response(status_code=204)


@router.post('/projects/{project_id}/images', response_model=ProjectDetail)
def upload_image(project_id: str, request: Request, person: Literal['a', 'b'] = Form(...),
                 angle: Literal['front', 'left', 'right', 'left_full', 'right_full'] = Form('front'), file: UploadFile = File(...)):
    content = file.file.read()
    try:
        image_format(content)
    except ValueError as exc:
        error(422, 'invalid_image', str(exc))
    # Check the face and crop to head and shoulders before anything is stored (app/photo_prep.py).
    try:
        prepared = request.app.state.photo_preparer.prepare(content)
    except PhotoRejected as exc:
        error(422, 'unusable_photo', exc.message)
    except (OSError, ValueError, Image.DecompressionBombError):
        error(422, 'invalid_image', '无法读取这张照片，请换一张后重试')
    upload_id, asset_id, now = new_id(), new_id(), utc_now()
    with request.app.state.db.connect(write=True) as conn:
        project = get_project(conn, project_id)
        try:
            # The untouched upload is kept so photos can be prepared again if the rules change.
            upload_path = save_image(request.app.state.config, project_id, upload_id, content, original=True)
            relative = save_image(request.app.state.config, project_id, asset_id, prepared, original=True)
        except OSError:
            logger.exception('Upload storage failed for project %s', project_id)
            error(507, 'storage_error', '照片保存失败，请检查存储空间及目录权限')
        conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at) VALUES (?,?,?,?,?)',
                     (upload_id, project_id, upload_path, 'upload', now))
        # 'original' marks a person photo used as a reference (stale-photo checks rely on it).
        conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at,source_asset_id) VALUES (?,?,?,?,?,?)',
                     (asset_id, project_id, relative, 'original', now, upload_id))
        conn.execute(f'UPDATE projects SET {upload_column(person, angle)}=?,updated_at=? WHERE id=?', (asset_id, now, project_id))
        return project_response(conn, get_project(conn, project_id))


@router.delete('/projects/{project_id}/images/{person}/{angle}', status_code=204)
def delete_side_image(project_id: str, person: Literal['a', 'b'], angle: Literal['left', 'right', 'left_full', 'right_full'],
                      request: Request):
    with request.app.state.db.connect(write=True) as conn:
        get_project(conn, project_id)
        conn.execute(f'UPDATE projects SET {upload_column(person, angle)}=NULL,updated_at=? WHERE id=?', (utc_now(), project_id))
    return Response(status_code=204)


@router.get('/assets/{asset_id}')
def get_asset(asset_id: str, request: Request):
    with request.app.state.db.connect() as conn:
        asset = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (asset_id,)).fetchone()
        if asset is None:
            error(404, 'asset_not_found', '图片不存在')
        try:
            path = asset_path(request.app.state.config, asset['file_path'])
        except ValueError:
            error(404, 'asset_not_found', '图片路径无效')
        if not path.is_file():
            error(404, 'asset_missing', '图片文件缺失，请重新上传或重新生成')
        return FileResponse(path)


@router.post('/projects/{project_id}/previews', response_model=Batch, status_code=201)
def create_previews(project_id: str, body: PreviewCreate, request: Request, response: Response):
    with request.app.state.db.connect(write=True) as conn:
        project = get_project(conn, project_id)
        existing = conn.execute('SELECT * FROM generation_batches WHERE client_request_id=?', (body.client_request_id,)).fetchone()
        if existing:
            response.status_code = 200
            return batch_response(conn, existing)
        if not project['person_a_image_id'] or not project['person_b_image_id']:
            error(409, 'photos_missing', '请先上传两个人的照片')
        model_id = body.model if body.model is not None else request.app.state.db.get_default_model()
        model = get_model(model_id)
        if model is None or (body.model is not None and not _offered(request, body.model)):
            invalid_model()
        style_ids = body.style_ids if body.style_ids is not None else list(DEFAULT_PREVIEW_STYLE_IDS)
        if not style_ids or len(set(style_ids)) != len(style_ids) or any(style not in STYLE_BY_ID for style in style_ids):
            error(422, 'invalid_styles', '请选择有效且不重复的风格')
        # A retired style may only refresh a sample this project already has.
        retired = [style for style in style_ids if style in RETIRED_STYLE_IDS and conn.execute(
            "SELECT 1 FROM photo_slots WHERE project_id=? AND kind='preview' AND style_id=?", (project_id, style)).fetchone() is None]
        if retired:
            error(422, 'invalid_styles', '「' + '」「'.join(STYLE_BY_ID[style]['name'] for style in retired) + '」已停用，不能生成新的样片')
        # V7.34: a style with rooms is shot in the room the user picked, its first room when none is picked.
        # V7.56: a style may be shot in several of its rooms at once, a sample per room.
        rooms: dict[str, list[str]] = {}
        for style_id, picked in (body.rooms or {}).items():
            picked = [picked] if isinstance(picked, str) else picked
            if (style_id not in style_ids or not picked or len(set(picked)) != len(picked)
                    or any(room_id not in {room.id for room in style_rooms(style_id)} for room_id in picked)):  # V7.58: not retired
                error(422, 'invalid_room', f'「{STYLE_BY_ID[style_id]["name"] if style_id in STYLE_BY_ID else style_id}」没有这个场景，请重新选择')
            rooms[style_id] = picked
        for style_id in style_ids:
            if has_rooms(style_id):
                rooms.setdefault(style_id, [default_room(style_id)])
        # V7.37: an outfit picked with the room is checked here and saved as the project's choice below.
        for style_id, outfit_id in (body.outfits or {}).items():
            if style_id not in style_ids or outfit_id not in STYLE_OUTFITS.get(style_id, ()):
                error(422, 'invalid_outfit', f'「{STYLE_BY_ID[style_id]["name"] if style_id in STYLE_BY_ID else style_id}」没有这套服饰，请重新选择')
        # Each sample uses its style's selected bride look and the selected groom look (V7.0).
        selected = style_looks(conn, project)
        for style_id, outfit_id in (body.outfits or {}).items():
            conn.execute('INSERT INTO project_outfits (project_id,style_id,outfit_id) VALUES (?,?,?) '
                         'ON CONFLICT(project_id,style_id) DO UPDATE SET outfit_id=excluded.outfit_id', (project_id, style_id, outfit_id))
        outfits = style_outfits(conn, project)
        pairs = {}
        for style_id in style_ids:
            bride, groom = selected[style_id]['bride'], selected[style_id]['groom']
            if bride is None or groom is None:
                who = '新娘和新郎' if bride is None and groom is None else '新娘' if bride is None else '新郎'
                error(409, 'looks_required', f'「{STYLE_BY_ID[style_id]["name"]}」还没有选用{who}的造型，请先在「风格与造型」里做好并选用')
            pairs[style_id] = (_get_look(conn, bride), _get_look(conn, groom))
        references, _ = _build_references(project)
        if len(references) > model.max_reference_images:
            error(422, 'invalid_model', '所选模型支持的参考照片数量不足')
        batch_id, now = new_id(), utc_now()
        conn.execute('INSERT INTO generation_batches (id,project_id,batch_type,client_request_id,created_at) VALUES (?,?,?,?,?)', (batch_id, project_id, 'preview', body.client_request_id, now))
        # Samples are always portrait; the director decides frames only for photo sets.
        preview_ratio = _shot_ratio(request, 'portrait')
        for style_id, room in [(style_id, room) for style_id in style_ids for room in rooms.get(style_id, [None])]:
            looks = pairs[style_id]
            references, _ = _build_references(project, looks=looks)
            # A sample is shot in its room's first area, the room's main picture.
            area = 1 if room is not None else None
            art, needed = _add_style_art(conn, request, project_id, style_id, outfits.get(style_id), references, room, area)
            if needed > model.max_reference_images:
                error(422, 'invalid_model', '所选模型支持的参考照片数量不足')
            snapshot = _fresh_snapshot(request, model_id, preview_ratio, looks, outfit=outfits.get(style_id),
                                       art_style=style_id if art else None, room=room, area=area)
            slot = conn.execute("SELECT id FROM photo_slots WHERE project_id=? AND kind='preview' AND style_id=? AND room_id IS ?",
                                (project_id, style_id, room)).fetchone()
            slot_id = slot['id'] if slot else new_id()
            if slot is None:
                conn.execute('INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,room_id,created_at) VALUES (?,?,?,?,?,?,?)',
                             (slot_id, project_id, batch_id, 'preview', style_id, room, now))
            conn.execute(
                'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,prompt,model_config_snapshot,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (new_id(), project_id, batch_id, slot_id, 'waiting', json.dumps(references), style_id,
                 pair_looks(compose_preview_prompt(style_id, preview_ratio, styled=True, outfit=outfits.get(style_id),
                                                   makeup_style=makeup_style_of(looks[0]), art=art, room=room,
                                                   jewelry=looks[0]['jewelry_id'] is not None)), snapshot, utc_now()),
            )
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, project_id))
        result = batch_response(conn, get_batch(conn, batch_id))
    request.app.state.task_manager.notify()
    return result


@router.post('/projects/{project_id}/moment-drafts', response_model=MomentDraft, status_code=201)
def create_moment_draft(project_id: str, body: MomentDraftCreate, request: Request):
    preview_slot_id = body.preview_slot_id
    count = body.count
    if count not in (4, 8, 12, 20):
        error(422, 'invalid_count', '拍摄数量必须是 4、8、12 或 20')
    with request.app.state.db.connect(write=True) as conn:
        project = get_project(conn, project_id)
        if not project['person_a_image_id'] or not project['person_b_image_id']:
            error(409, 'photos_missing', '请先上传两个人的照片')
        preview = conn.execute('SELECT * FROM photo_slots WHERE id=? AND project_id=? AND kind=\'preview\'', (preview_slot_id, project_id)).fetchone()
        if preview is None:
            error(404, 'slot_not_found', '样片位置不存在')
        if preview['current_version_id'] is None:
            error(409, 'slot_not_ready', '样片尚未生成成功，请等待完成后再拍组')
        avoid = [row['moment_title'] for row in conn.execute(
            'SELECT DISTINCT moment_title FROM generation_tasks WHERE project_id=? AND style_id=? AND moment_title IS NOT NULL',
            (project_id, preview['style_id'])) if row['moment_title']]
        style = STYLE_BY_ID[preview['style_id']]
        source = _slot_source_task(conn, preview)
        outfit = _source_outfit(source, f'「{style["name"]}」样片')
        # V7.34: the set is shot in the sample's room, over the room's areas.
        room, _area = _source_room(source, f'「{style["name"]}」样片')
        # With the preview or the style art as a reference image, every shot stays in one scene (docs/prompts.md 7.2).
        single_variant = _single_scene(request, preview['style_id'], outfit, room)
        scene = director_scene_text(preview['style_id'], single_variant=single_variant, outfit=outfit, room=room)
        result = _director_write(request, style=style['name'], scene=scene,
                                 images=_director_images(request, preview['style_id'], room, single_variant),
                                 scene_count=director_scene_count(preview['style_id'], single_variant=single_variant, room=room),
                                 count=count,
                                 shot_requirement=_shot_requirement(count, atmospheric=style.get('atmospheric', False)), avoid_titles=avoid,
                                 facings=_project_facings(project))
        now, draft_id = utc_now(), new_id()
        director_model = request.app.state.db.get_director_model() or ('mock' if request.app.state.config.generator == 'mock' else None)
        conn.execute('INSERT INTO moment_drafts (id,project_id,preview_slot_id,style_id,director_model,moments,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                     (draft_id, project_id, preview_slot_id, preview['style_id'], director_model,
                      json.dumps([moment_dict(item) for item in result], ensure_ascii=False), now, now))
        row = conn.execute('SELECT * FROM moment_drafts WHERE id=?', (draft_id,)).fetchone()
        return _draft_response(row, [moment_dict(item) for item in result])


@router.get('/moment-drafts/{draft_id}', response_model=MomentDraft)
def get_moment_draft(draft_id: str, request: Request):
    with request.app.state.db.connect() as conn:
        row, moments = _load_draft(conn, draft_id)
        return _draft_response(row, [moment_dict(item) for item in moments])


@router.patch('/moment-drafts/{draft_id}/moments/{index}', response_model=MomentDraft)
def patch_moment(draft_id: str, index: int, body: MomentPatch, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        row, moments = _load_draft(conn, draft_id)
        if row['used_by_batch_id']:
            error(409, 'draft_used', '导演方案已经用于拍摄，不能再修改')
        if index < 0 or index >= len(moments):
            error(404, 'moment_not_found', '动作瞬间不存在')
        value = moment_dict(moments[index])
        value.update(body.model_dump(exclude_unset=True))
        candidate = Moment(**value)
        problems = validate_moment(candidate, avoid_titles=[item.title for position, item in enumerate(moments) if position != index])
        if problems:
            error(422, 'invalid_moment', '；'.join(problems))
        moments[index] = candidate
        now = utc_now()
        conn.execute('UPDATE moment_drafts SET moments=?,updated_at=? WHERE id=?',
                     (json.dumps([moment_dict(item) for item in moments], ensure_ascii=False), now, draft_id))
        row = conn.execute('SELECT * FROM moment_drafts WHERE id=?', (draft_id,)).fetchone()
        return _draft_response(row, [moment_dict(item) for item in moments])


@router.post('/moment-drafts/{draft_id}/moments/{index}/rewrite', response_model=MomentDraft)
def rewrite_moment(draft_id: str, index: int, body: MomentRewrite, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        row, moments = _load_draft(conn, draft_id)
        if row['used_by_batch_id']:
            error(409, 'draft_used', '导演方案已经用于拍摄，不能再修改')
        if index < 0 or index >= len(moments):
            error(404, 'moment_not_found', '动作瞬间不存在')
        preview = conn.execute('SELECT * FROM photo_slots WHERE id=?', (row['preview_slot_id'],)).fetchone()
        style = STYLE_BY_ID[row['style_id']]
        facings = _project_facings(get_project(conn, row['project_id']))
        source = _slot_source_task(conn, preview) if preview is not None else None
        outfit = _source_outfit(source, f'「{style["name"]}」样片') if source is not None else None
        room = _source_room(source, f'「{style["name"]}」样片')[0] if source is not None else None
        single_variant = _single_scene(request, row['style_id'], outfit, room)
        scene = director_scene_text(row['style_id'], single_variant=single_variant, outfit=outfit, room=room)
        current = moments[index]
        avoid = [item.title for item in moments]
        if body.shot_size:
            replacement = _director_rewrite_shot(request, style=style['name'], scene=scene, original=current.text,
                                                 images=_director_images(request, row['style_id'], room, single_variant),
                                                 target=body.shot_size, orientation=current.orientation, facings=facings)
            # Changing the shot size keeps the scene and whether it is an atmosphere shot.
            replacement = replace(replacement, scene=current.scene, faces_clear=current.faces_clear)
        else:
            replacement = _director_write(request, style=style['name'], scene=scene,
                                          images=_director_images(request, row['style_id'], room, single_variant),
                                          scene_count=director_scene_count(row['style_id'], single_variant=single_variant, room=room),
                                          count=1,
                                          shot_requirement=f'保持 {SHOT_NAMES[current.shot_size]}。',
                                          avoid_titles=avoid, facings=facings)[0]
        problems = validate_moment(replacement, avoid_titles=[item.title for item in moments if item is not current], target_shot=body.shot_size,
                                   target_orientation=current.orientation if body.shot_size else None, check_facing_text=True,
                                   allowed_facings=facings)
        if problems:
            error(502, 'director_invalid_output', 'AI 导演没有写出符合要求的方案，请重试')
        moments[index] = replacement
        now = utc_now()
        conn.execute('UPDATE moment_drafts SET moments=?,updated_at=? WHERE id=?',
                     (json.dumps([moment_dict(item) for item in moments], ensure_ascii=False), now, draft_id))
        row = conn.execute('SELECT * FROM moment_drafts WHERE id=?', (draft_id,)).fetchone()
        return _draft_response(row, [moment_dict(item) for item in moments])


@router.post('/projects/{project_id}/photo-batches', response_model=Batch, status_code=201)
def create_photo_batch(project_id: str, body: PhotoBatchCreate, request: Request, response: Response):
    with request.app.state.db.connect(write=True) as conn:
        project = get_project(conn, project_id)
        existing = conn.execute('SELECT * FROM generation_batches WHERE client_request_id=?', (body.client_request_id,)).fetchone()
        if existing:
            response.status_code = 200
            return batch_response(conn, existing)
        if not project['person_a_image_id'] or not project['person_b_image_id']:
            error(409, 'photos_missing', '请先上传两个人的照片')
        draft, moments = _load_draft(conn, body.draft_id)
        if draft['project_id'] != project_id:
            error(404, 'draft_not_found', '导演方案不存在')
        if draft['used_by_batch_id']:
            error(409, 'draft_used', '导演方案已经用于拍摄，不能重复使用')
        preview_id = draft['preview_slot_id']
        preview = conn.execute("SELECT * FROM photo_slots WHERE id=? AND project_id=? AND kind='preview'", (preview_id, project_id)).fetchone()
        if preview is None:
            error(404, 'slot_not_found', '样片位置不存在')
        if preview['current_version_id'] is None:
            error(409, 'slot_not_ready', '样片尚未生成成功，请等待完成后再拍组')
        model_id = body.model if body.model is not None else request.app.state.db.get_default_model()
        model = get_model(model_id)
        if model is None or (body.model is not None and not _offered(request, body.model)):
            invalid_model()
        mode = request.app.state.db.get_batch_reference_mode()
        preview_version = preview['current_version_id']
        # A photo set is shot in its sample's looks (V5.0).
        preview_source = _slot_source_task(conn, preview)
        looks = _source_looks(conn, project, preview_source, f'「{STYLE_BY_ID[preview["style_id"]]["name"]}」样片')
        outfit = _source_outfit(preview_source, f'「{STYLE_BY_ID[preview["style_id"]]["name"]}」样片')
        # V7.34: the set stays in the sample's room; each shot is in the area the director chose for it.
        room, _area = _source_room(preview_source, f'「{STYLE_BY_ID[preview["style_id"]]["name"]}」样片')
        if room is not None and any(not 1 <= moment.scene <= len(get_room(preview['style_id'], room).areas) for moment in moments):
            error(409, 'draft_stale', '导演方案的场景与样片所在的场景不一致，请重新生成导演方案')
        # V7.33: the style art replaces the sample as the reference for the clothes and the place.
        art = style_art_applies(request.app.state.db, preview['style_id'], outfit)
        with_preview = mode == 'with_preview' and not art
        preview_asset = None
        if with_preview:
            preview_asset = conn.execute('SELECT image_asset_id FROM photo_versions WHERE id=?', (preview_version,)).fetchone()['image_asset_id']
        prepared_references = []
        offending_shots = []
        for moment in moments:
            references, side_references = _build_references(project, moment.bride_facing, moment.groom_facing, preview_asset, looks=looks)
            _, needed = _add_style_art(conn, request, project_id, preview['style_id'], outfit, references, room,
                                       moment.scene if room is not None else None)
            prepared_references.append((references, side_references, needed))
            if needed > model.max_reference_images:
                offending_shots.append(moment.title)
        if offending_shots:
            required = max(needed for _, _, needed in prepared_references)
            error(422, 'invalid_model', f'以下镜头需要 {required} 张参考图，超过所选模型上限：{"、".join(offending_shots)}。请切换模型或关闭「' + ('服饰场景参考图' if art else '带样片参考') + '」')
        batch_id, now = new_id(), utc_now()
        conn.execute('INSERT INTO generation_batches (id,project_id,batch_type,client_request_id,created_at,style_id,reference_preview_version_id) VALUES (?,?,?,?,?,?,?)', (batch_id, project_id, 'photo', body.client_request_id, now, preview['style_id'], preview_version))
        for moment, (references, side_references, _needed) in zip(moments, prepared_references):
            aspect_ratio = _shot_ratio(request, moment.orientation)
            area = moment.scene if room is not None else None
            snapshot = _fresh_snapshot(request, model_id, aspect_ratio, looks, side_references, with_preview, outfit,
                                       preview['style_id'] if art else None, room, area)
            slot_id, task_id = new_id(), new_id()
            conn.execute('INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,pose_id,pose_name,shot_size,created_at) VALUES (?,?,?,?,?,?,?,?,?)', (slot_id, project_id, batch_id, 'photo', preview['style_id'], None, moment.title, moment.shot_size, utc_now()))
            prompt = compose_moment_prompt(preview['style_id'], moment.text, aspect_ratio, scene=moment.scene,
                                           faces_clear=moment.faces_clear, with_preview=with_preview,
                                           side_references=side_references, styled=True, outfit=outfit,
                                           makeup_style=makeup_style_of(looks[0]), art=art, room=room,
                                           jewelry=looks[0]['jewelry_id'] is not None)
            prompt = pair_looks(prompt)
            conn.execute('INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,pose_id,moment_title,moment_shot_size,prompt,model_config_snapshot,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (task_id, project_id, batch_id, slot_id, 'waiting', json.dumps(references), preview['style_id'], None, moment.title, moment.shot_size, prompt, snapshot, utc_now()))
        conn.execute('UPDATE moment_drafts SET used_by_batch_id=?,updated_at=? WHERE id=?', (batch_id, now, body.draft_id))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, project_id))
        result = batch_response(conn, get_batch(conn, batch_id))
    request.app.state.task_manager.notify()
    return result


@router.post('/slots/{slot_id}/regenerate', response_model=Batch, status_code=201)
def regenerate_slot(slot_id: str, body: RegenerateCreate, request: Request, response: Response):
    with request.app.state.db.connect(write=True) as conn:
        existing = conn.execute(
            'SELECT * FROM generation_batches WHERE client_request_id=?', (body.client_request_id,)
        ).fetchone()
        if existing:
            response.status_code = 200
            return batch_response(conn, existing)
        slot = get_slot(conn, slot_id)
        project = get_project(conn, slot['project_id'])
        spec = _prepare_slot_action(
            conn, slot, project, body.mode, body.model, request,
            slot_label=_slot_user_label(conn, slot),
        )
        result = _insert_regenerate_batch(conn, project['id'], body.client_request_id, [spec])
    request.app.state.task_manager.notify()
    return result


@router.post('/slots/{slot_id}/face-swap', response_model=Batch, status_code=201)
def face_swap_slot(slot_id: str, body: FaceSwapCreate, request: Request, response: Response):
    with request.app.state.db.connect(write=True) as conn:
        existing = conn.execute(
            'SELECT * FROM generation_batches WHERE client_request_id=?', (body.client_request_id,)
        ).fetchone()
        if existing:
            response.status_code = 200
            return batch_response(conn, existing)
        slot = get_slot(conn, slot_id)
        project = get_project(conn, slot['project_id'])
        spec = _prepare_face_swap_spec(conn, slot, project, request, slot_label=_slot_action_label(conn, slot))
        result = _insert_regenerate_batch(conn, project['id'], body.client_request_id, [spec])
    request.app.state.task_manager.notify()
    return result


@router.post('/slots/{slot_id}/face-fix', response_model=Batch, status_code=201)
def face_fix_slot(slot_id: str, body: FaceFixCreate, request: Request, response: Response):
    """AI face fix of one picture (V7.1): one paid edit of the whole picture (V7.2)."""
    with request.app.state.db.connect(write=True) as conn:
        existing = conn.execute(
            'SELECT * FROM generation_batches WHERE client_request_id=?', (body.client_request_id,)
        ).fetchone()
        if existing:
            response.status_code = 200
            return batch_response(conn, existing)
        slot = get_slot(conn, slot_id)
        project = get_project(conn, slot['project_id'])
        spec = _prepare_face_fix_spec(conn, slot, project, request, slot_label=_slot_action_label(conn, slot),
                                      explicit_model=body.model)
        result = _insert_regenerate_batch(conn, project['id'], body.client_request_id, [spec])
    request.app.state.task_manager.notify()
    return result


def _picture_edit(slot_id: str, body: FaceFixCreate, request: Request, response: Response, mode: str):
    with request.app.state.db.connect(write=True) as conn:
        existing = conn.execute(
            'SELECT * FROM generation_batches WHERE client_request_id=?', (body.client_request_id,)
        ).fetchone()
        if existing:
            response.status_code = 200
            return batch_response(conn, existing)
        slot = get_slot(conn, slot_id)
        project = get_project(conn, slot['project_id'])
        spec = _prepare_picture_edit_spec(conn, slot, project, request, mode, slot_label=_slot_action_label(conn, slot),
                                          explicit_model=body.model)
        result = _insert_regenerate_batch(conn, project['id'], body.client_request_id, [spec])
    request.app.state.task_manager.notify()
    return result


@router.post('/slots/{slot_id}/hand-fix', response_model=Batch, status_code=201)
def hand_fix_slot(slot_id: str, body: FaceFixCreate, request: Request, response: Response):
    """Hand fix of one picture (V7.25): one paid edit of the whole picture that corrects only the hands."""
    return _picture_edit(slot_id, body, request, response, 'hand_fix')


@router.post('/slots/{slot_id}/wash', response_model=Batch, status_code=201)
def wash_slot(slot_id: str, body: FaceFixCreate, request: Request, response: Response):
    """Wash of one picture (V7.26): one paid 4K edit that redraws the whole picture cleaner and sharper."""
    return _picture_edit(slot_id, body, request, response, 'wash')


@router.post('/slots/{slot_id}/hand-analysis', response_model=HandAnalysisRecord)
def analyze_slot_hands(slot_id: str, request: Request,
                       source_version_id: str = Form(..., min_length=1, max_length=128),
                       annotation_kind: Literal['crop', 'reference', 'markup', 'mask'] = Form('crop'),
                       instruction: str = Form('', max_length=2000),
                       annotation: UploadFile | None = File(None)):
    """One explicit vision request; an absent annotation asks for candidate boxes, not an edit."""
    db, config = request.app.state.db, request.app.state.config
    with db.connect() as conn:
        slot = get_slot(conn, slot_id)
        if slot['current_version_id'] != source_version_id:
            error(409, 'source_changed', '照片版本已改变，请重新打开编辑器后分析')
        source = conn.execute(
            'SELECT asset.file_path FROM photo_versions version JOIN image_assets asset ON asset.id=version.image_asset_id '
            'WHERE version.id=? AND version.photo_slot_id=?', (source_version_id, slot_id),
        ).fetchone()
        if source is None:
            error(409, 'slot_not_ready', '照片尚无可分析的成功版本')
    provider, model = db.get_director_provider(), db.get_director_model()
    if config.generator != 'mock' and not model:
        error(409, 'hand_analysis_unavailable', '请先在设置中填写支持看图的 AI 导演模型；手部诊断复用该模型及服务')
    problem = _director_provider_problem(request, provider)
    if problem:
        error(409, 'hand_analysis_unavailable', problem)
    content = None
    path = asset_path(config, source['file_path'])
    try:
        if annotation is not None:
            content = validate_annotation(annotation.file.read(MAX_ANNOTATION_BYTES + 1), oriented_size(path), annotation_kind)
        parts, size = analysis_inputs(path, content, annotation_kind)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        error(422, 'invalid_annotation' if annotation is not None else 'invalid_image', str(exc))
    if instruction.strip():
        parts.append({'type': 'text', 'text': '用户希望修改的目标（不是已确认的图像事实，请独立核查）：' + instruction.strip()})
    try:
        report = request.app.state.hand_analyzer.analyze(parts, model=model or 'mock', provider=provider, selection=content is not None)
    except HandAnalysisError as exc:
        error(502, exc.code, exc.message)
    # A long analysis must not become a diagnosis of a newer version by accident.
    with db.connect() as conn:
        if get_slot(conn, slot_id)['current_version_id'] != source_version_id:
            error(409, 'source_changed', '分析期间照片版本已改变，请重新打开编辑器；本次分析可能已计费')
    return HandAnalysisRecord(**report.model_dump(), source_version_id=source_version_id,
        scope='selection' if content is not None else 'detect', model=model or 'mock', provider=provider,
        image_size=list(size), annotation_sha256=hashlib.sha256(content).hexdigest() if content is not None else None,
        annotation_kind=annotation_kind if content is not None else None, created_at=utc_now(),
        repair_prompts=[region.repair_prompt() for region in report.regions])


@router.post('/slots/{slot_id}/regional-edit', response_model=Batch, status_code=201)
def regional_edit_slot(slot_id: str, request: Request, response: Response,
                       client_request_id: str = Form(..., min_length=1, max_length=128),
                       source_version_id: str = Form(..., min_length=1),
                       prompt: str = Form(..., min_length=1, max_length=2000),
                       model: str = Form(...), provider: Literal['88api', 'pptoken', 'ark'] = Form(...),
                       method: Literal['crop', 'native', 'reference'] = Form('native'),
                       hand_analysis: str | None = Form(None, max_length=20000),
                       markup: UploadFile | None = File(None), mask: UploadFile | None = File(None)):
    """One annotation of the version visible in the editor; duplicates never create a second paid task."""
    if (markup is None) == (mask is None):
        error(422, 'invalid_annotation', '请提供一个标记图或蒙版，两者不能同时提交')
    if method in ('reference', 'crop') and mask is not None:
        error(422, 'invalid_annotation', '这种编辑方式需要标记图，不能使用蒙版')
    if method == 'native' and markup is not None and provider in UNSUPPORTED_MARKUP_PROVIDERS:
        error(422, 'markup_unsupported', 'PPTOKEN 当前不支持原生 markup，请明确选择标记参考方式')
    prompt = prompt.strip()
    if not prompt:
        error(422, 'invalid_prompt', '请描述需要修改的内容')
    definition = get_model(model)
    if definition is None or model not in REGIONAL_EDIT_MODELS or provider not in definition.providers:
        error(422, 'invalid_model', '请选择所选服务支持的局部编辑模型（GPT Image 2.5、2.6、Nano Banana 2.1 或 Seedream 5.5）')
    if model in CROP_ONLY_MODELS and method != 'crop':
        error(422, 'method_unsupported', f'{definition.name} 只支持「放大局部修」，请换这种方式或换 GPT Image 模型')
    problem = _provider_problem(request, provider)
    if problem:
        error(422, 'provider_unavailable', problem)
    kind, upload = ((method if method in ('reference', 'crop') else 'markup', markup) if markup is not None
                    else ('mask', mask))
    content = upload.file.read(MAX_ANNOTATION_BYTES + 1)
    if not content or len(content) > MAX_ANNOTATION_BYTES:
        error(422, 'invalid_annotation', '标记图不能为空，且不能超过 50 MB')
    diagnosis = None
    if hand_analysis is not None:
        try:
            diagnosis = HandAnalysisRecord.model_validate(json.loads(hand_analysis))
        except (ValueError, TypeError):
            error(422, 'invalid_hand_analysis', '手部诊断记录无效，请重新分析或使用手动修改要求')
        if diagnosis.source_version_id != source_version_id or diagnosis.selected_region_index is None:
            error(422, 'invalid_hand_analysis', '手部诊断与本次修复目标不一致，请重新分析')
    fingerprint = hashlib.sha256(json.dumps(
        [slot_id, source_version_id, prompt, model, provider, kind, hashlib.sha256(content).hexdigest(),
         *([diagnosis.model_dump()] if diagnosis is not None else [])],
        ensure_ascii=False, separators=(',', ':'),
    ).encode()).hexdigest()
    config = request.app.state.config
    relative, committed = None, False
    try:
        with request.app.state.db.connect(write=True) as conn:
            existing = conn.execute('SELECT * FROM generation_batches WHERE client_request_id=?', (client_request_id,)).fetchone()
            if existing:
                task = conn.execute('SELECT * FROM generation_tasks WHERE batch_id=? LIMIT 1', (existing['id'],)).fetchone()
                if task is None or json.loads(task['model_config_snapshot']).get('request_fingerprint') != fingerprint:
                    error(409, 'request_conflict', '这个请求编号已经用于其他编辑，请重新提交')
                response.status_code = 200
                return batch_response(conn, existing)
            slot = get_slot(conn, slot_id)
            if slot['current_version_id'] is None:
                error(409, 'slot_not_ready', '照片尚无成功版本，暂时无法局部编辑')
            if slot['current_version_id'] != source_version_id:
                error(409, 'source_changed', '照片版本已改变，请重新打开编辑器并标记')
            busy = _slot_busy_task(conn, slot_id)
            if busy is not None:
                message = ('这张照片还有已暂停的任务，不会自动完成；请先取消暂停任务，或在任务中心继续任务后等待完成'
                           if busy['status'] == 'held' else '这张照片已有任务正在排队或生成，请等它完成后再编辑')
                error(409, 'slot_busy', message)
            current, shot = _version_task(conn, source_version_id), _slot_source_task(conn, slot)
            source = conn.execute(
                'SELECT asset.file_path FROM photo_versions version JOIN image_assets asset ON asset.id=version.image_asset_id '
                'WHERE version.id=? AND version.photo_slot_id=?', (source_version_id, slot_id),
            ).fetchone()
            if current is None or shot is None or source is None:
                error(409, 'slot_not_ready', '原图版本记录不完整，暂时无法局部编辑')
            try:
                size = oriented_size(asset_path(config, source['file_path']))
                annotation = validate_annotation(content, size, kind)
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                error(422, 'invalid_annotation', str(exc))
            if diagnosis is not None:
                if diagnosis.image_size != list(size) or (diagnosis.scope == 'selection' and (
                    diagnosis.annotation_kind != kind or diagnosis.annotation_sha256 != hashlib.sha256(annotation).hexdigest())):
                    error(422, 'invalid_hand_analysis', '标记或编辑方式已改变，请重新分析或使用手动修改要求')
            asset_id, now = new_id(), utc_now()
            try:
                relative = save_image(config, slot['project_id'], asset_id, annotation)
            except OSError:
                logger.exception('Annotation storage failed for slot %s', slot_id)
                error(507, 'storage_error', '标记保存失败，请检查存储空间及目录权限')
            conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at) VALUES (?,?,?,?,?)',
                         (asset_id, slot['project_id'], relative, 'edit_' + kind, now))
            # V7.79 'crop': the marked area is edited enlarged to 1024x1024 and laid back at the source size.
            output = ({'size': '1024x1024'} if kind == 'crop' else
                      {'size': 'auto', 'quality': 'medium', 'output_format': 'png'})
            snapshot = {'generator': 'regional_edit', 'model': model, 'provider': provider,
                        'aspect_ratio': _source_aspect_ratio(shot, request), **output,
                        'annotation_kind': kind, 'annotation_asset_id': asset_id,
                        'source_size': list(size), 'request_fingerprint': fingerprint}
            if diagnosis is not None:
                snapshot['hand_analysis'] = diagnosis.model_dump()
            spec = {'slot_id': slot_id, 'style_id': shot['style_id'], 'pose_id': shot['pose_id'],
                    'moment_title': shot['moment_title'], 'moment_shot_size': shot['moment_shot_size'],
                    'references': json.dumps(_parse_json_list(current['reference_image_ids'], '原图')[:2]),
                    'prompt': prompt, 'snapshot': json.dumps(snapshot, ensure_ascii=False),
                    'regenerate_mode': 'regional_edit', 'source_version_id': source_version_id}
            result = _insert_regenerate_batch(conn, slot['project_id'], client_request_id, [spec])
        committed = True
    finally:
        if relative is not None and not committed:
            asset_path(config, relative).unlink(missing_ok=True)
    request.app.state.task_manager.notify()
    return result


@router.post('/slots/{slot_id}/change-pose', response_model=Batch, status_code=201)
def change_pose(slot_id: str, body: ChangePoseCreate, request: Request, response: Response):
    with request.app.state.db.connect(write=True) as conn:
        existing = conn.execute(
            'SELECT * FROM generation_batches WHERE client_request_id=?', (body.client_request_id,)
        ).fetchone()
        if existing:
            response.status_code = 200
            return batch_response(conn, existing)
        slot = get_slot(conn, slot_id)
        if slot['kind'] != 'photo':
            error(409, 'slot_not_ready', f'{_slot_user_label(conn, slot)}不是相册作品，不能更换动作')
        project = get_project(conn, slot['project_id'])
        source = _slot_source_task(conn, slot)
        selected_pose = _choose_pose(conn, project['id'], slot['style_id'], slot, set()) if source is not None and source['moment_title'] is None else None
        spec = _prepare_slot_action(
            conn, slot, project, 'change_pose', body.model, request,
            slot_label=_slot_user_label(conn, slot), selected_pose=selected_pose,
        )
        result = _insert_regenerate_batch(conn, project['id'], body.client_request_id, [spec])
    request.app.state.task_manager.notify()
    return result


@router.post('/slots/{slot_id}/change-shot', response_model=Batch, status_code=201)
def change_shot(slot_id: str, body: ChangeShotCreate, request: Request, response: Response):
    with request.app.state.db.connect(write=True) as conn:
        existing = conn.execute('SELECT * FROM generation_batches WHERE client_request_id=?', (body.client_request_id,)).fetchone()
        if existing:
            response.status_code = 200
            return batch_response(conn, existing)
        slot = get_slot(conn, slot_id)
        project = get_project(conn, slot['project_id'])
        spec = _prepare_slot_action(conn, slot, project, 'change_shot', body.model, request,
                                    slot_label=_slot_user_label(conn, slot), target_shot=body.shot_size)
        result = _insert_regenerate_batch(conn, project['id'], body.client_request_id, [spec])
    request.app.state.task_manager.notify()
    return result


@router.post('/slots/batch-action', response_model=Batch, status_code=201)
def batch_action(body: BatchActionCreate, request: Request, response: Response):
    with request.app.state.db.connect(write=True) as conn:
        existing = conn.execute(
            'SELECT * FROM generation_batches WHERE client_request_id=?', (body.client_request_id,)
        ).fetchone()
        if existing:
            response.status_code = 200
            return batch_response(conn, existing)
        if not body.slot_ids:
            error(422, 'invalid_slots', '请至少选择一张照片')
        if len(set(body.slot_ids)) != len(body.slot_ids):
            error(422, 'invalid_slots', '作品位置不能重复')
        slots = []
        project_id = None
        for slot_id in body.slot_ids:
            slot = conn.execute('SELECT * FROM photo_slots WHERE id=?', (slot_id,)).fetchone()
            if slot is None:
                error(422, 'invalid_slots', '所选作品位置不存在')
            if project_id is None:
                project_id = slot['project_id']
            elif slot['project_id'] != project_id:
                error(422, 'invalid_slots', '所选作品位置必须属于同一个项目')
            slots.append(slot)
        project = get_project(conn, project_id)
        photo_positions = {
            slot['id']: index for index, slot in enumerate(_ordered_photo_slots(conn, project_id), 1)
        }
        # Complete all validation before inserting the batch or any task.
        if body.action == 'change_shot' and body.shot_size is None:
            error(422, 'invalid_shot_size', '批量换景别必须提供 shot_size')
        specs = []
        generated_titles: list[str] = []
        generated_legacy_poses: set[str] = set()
        for slot in slots:
            slot_label = _slot_user_label(conn, slot, position=photo_positions.get(slot['id']))
            if body.action == 'face_swap':
                spec = _prepare_face_swap_spec(conn, slot, project, request, slot_label=slot_label)
            elif body.action == 'face_fix':
                spec = _prepare_face_fix_spec(conn, slot, project, request, slot_label=slot_label, explicit_model=body.model)
            else:
                source = _slot_source_task(conn, slot)
                selected_pose = _choose_pose(conn, project_id, slot['style_id'], slot, generated_legacy_poses) if body.action == 'change_pose' and source is not None and source['moment_title'] is None else None
                if selected_pose:
                    generated_legacy_poses.add(selected_pose)
                spec = _prepare_slot_action(
                    conn, slot, project, body.action, body.model, request,
                    slot_label=slot_label,
                    target_shot=body.shot_size, avoid_extra=generated_titles, selected_pose=selected_pose,
                )
            generated_titles.extend([spec['moment_title']] if spec.get('moment_title') else [])
            specs.append(spec)
        result = _insert_regenerate_batch(conn, project_id, body.client_request_id, specs)
    request.app.state.task_manager.notify()
    return result


@router.get('/slots/{slot_id}/versions', response_model=list[VersionDetail])
def slot_versions(slot_id: str, request: Request):
    with request.app.state.db.connect() as conn:
        slot = get_slot(conn, slot_id)
        rows = conn.execute(
            'SELECT version.id,version.image_asset_id AS asset_id,version.version_number,version.created_at,version.face_scores,version.size_warning,'
            'task.model_config_snapshot,task.pose_id,task.moment_title,task.moment_shot_size,task.regenerate_mode,'
            'task.source_version_id '
            'FROM photo_versions version JOIN generation_tasks task ON task.id=version.generation_task_id '
            'WHERE version.photo_slot_id=? ORDER BY version.version_number DESC',
            (slot['id'],),
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            try:
                snapshot = json.loads(value.pop('model_config_snapshot'))
            except (TypeError, ValueError):
                snapshot = None
            pose_id = value['pose_id'] if slot['kind'] == 'photo' else None
            result.append({
                **value, 'model': snapshot.get('model') if isinstance(snapshot, dict) else None, 'pose_id': pose_id,
                'pose_name': value.get('moment_title') or (POSE_BY_ID.get(pose_id, {}).get('name') if pose_id else None),
                'shot_size': value.get('moment_shot_size'),
                'is_current': value['id'] == slot['current_version_id'],
                'face_scores': stored_face_scores(value['face_scores']),
            })
            if result[-1]['shot_size'] is None:
                result[-1].pop('shot_size')
        return result


@router.post('/slots/{slot_id}/restore', response_model=Slot)
def restore_slot(slot_id: str, body: RestoreCreate, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        slot = get_slot(conn, slot_id)
        version = conn.execute(
            'SELECT id FROM photo_versions WHERE id=? AND photo_slot_id=?', (body.version_id, slot_id)
        ).fetchone()
        if version is None:
            error(404, 'version_not_found', f'{_slot_user_label(conn, slot)}的版本不存在或不属于该作品位置')
        conn.execute('UPDATE photo_slots SET current_version_id=? WHERE id=?', (body.version_id, slot_id))
        project = get_project(conn, slot['project_id'])
        return slot_response(conn, get_slot(conn, slot_id), project)


@router.delete('/slots/{slot_id}', status_code=204)
def delete_slot(slot_id: str, request: Request):
    db, config = request.app.state.db, request.app.state.config
    files_to_remove: list[tuple[str, str]] = []
    with db.connect(write=True) as conn:
        slot = get_slot(conn, slot_id)
        slot_label = _slot_user_label(conn, slot)
        if slot['kind'] != 'photo':
            error(409, 'slot_not_deletable', f'{slot_label}不能删除')
        conn.execute(
            "UPDATE generation_tasks SET status='cancelled',finished_at=? WHERE target_slot_id=? "
            "AND status IN ('waiting','held')", (utc_now(), slot_id),
        )
        busy = conn.execute(
            "SELECT 1 FROM generation_tasks WHERE target_slot_id=? AND status='running' LIMIT 1", (slot_id,)
        ).fetchone() is not None
        if not busy:
            versions = conn.execute(
                'SELECT version.image_asset_id,asset.file_path FROM photo_versions version '
                'JOIN image_assets asset ON asset.id=version.image_asset_id WHERE version.photo_slot_id=?',
                (slot_id,),
            ).fetchall()
            version_files = [(row['image_asset_id'], row['file_path']) for row in versions]
            # Annotation assets are task inputs, not photo versions. Retried tasks share the same asset.
            annotations = conn.execute(
                'SELECT DISTINCT asset.id,asset.file_path FROM generation_tasks task JOIN image_assets asset '
                "ON asset.id=json_extract(task.model_config_snapshot,'$.annotation_asset_id') "
                "WHERE task.target_slot_id=? AND asset.project_id=? AND asset.asset_type IN ('edit_markup','edit_mask','edit_reference')",
                (slot_id, slot['project_id']),
            ).fetchall()
            version_files.extend((row['id'], row['file_path']) for row in annotations)
            conn.execute('UPDATE photo_slots SET current_version_id=NULL WHERE id=?', (slot_id,))
            conn.execute('UPDATE generation_tasks SET output_asset_id=NULL WHERE target_slot_id=?', (slot_id,))
            conn.execute('DELETE FROM photo_versions WHERE photo_slot_id=?', (slot_id,))
            conn.execute('DELETE FROM photo_slots WHERE id=?', (slot_id,))
            for asset_id, relative in version_files:
                conn.execute('UPDATE generation_tasks SET output_asset_id=NULL WHERE output_asset_id=?', (asset_id,))
                shared = conn.execute(
                    'SELECT 1 FROM photo_versions WHERE image_asset_id=? LIMIT 1', (asset_id,),
                ).fetchone()
                if shared is None:
                    conn.execute('DELETE FROM image_assets WHERE id=?', (asset_id,))
                    files_to_remove.append((asset_id, relative))
            conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), slot['project_id']))
    if busy:
        error(409, 'slot_busy', f'{slot_label}已有任务正在排队或生成，请等它完成后再操作')
    for asset_id, relative in files_to_remove:
        try:
            path = asset_path(config, relative)
            path.unlink(missing_ok=True)
        except Exception:
            logger.exception('Photo version file removal failed for asset %s', asset_id)
    return Response(status_code=204)


def _safe_filename(value: str, fallback: str) -> str:
    value = str(value or fallback).replace('/', '_').replace('\\', '_')
    value = ''.join(char for char in value if char.isprintable() and char not in '<>:"|?*')
    return value.strip('. ') or fallback


@router.post('/projects/{project_id}/export')
def export_project(project_id: str, request: Request, body: ExportCreate | None = Body(default=None)):
    body = body or ExportCreate()
    db, config = request.app.state.db, request.app.state.config
    with db.connect() as conn:
        project = get_project(conn, project_id)
        query = "SELECT s.*,v.image_asset_id,asset.file_path FROM photo_slots s JOIN photo_versions v ON v.id=s.current_version_id JOIN image_assets asset ON asset.id=v.image_asset_id WHERE s.project_id=? AND s.kind='photo' ORDER BY s.created_at,s.rowid"
        rows = conn.execute(query, (project_id,)).fetchall()
        if body.slot_ids is not None:
            if len(set(body.slot_ids)) != len(body.slot_ids):
                error(422, 'invalid_slots', '作品位置不能重复')
            by_id = {row['id']: row for row in rows}
            if any(slot_id not in by_id for slot_id in body.slot_ids):
                error(422, 'invalid_slots', '选择的作品位置不属于该项目或没有当前版本')
            selected_ids = set(body.slot_ids)
            rows = [row for row in rows if row['id'] in selected_ids]
        temp_dir = config.temp_dir
        temp_dir.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(prefix='export-', suffix='.zip', dir=temp_dir)
        os.close(file_descriptor)
        temp_path = Path(temp_name)
        try:
            with zipfile.ZipFile(temp_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for index, row in enumerate(rows, 1):
                    path = asset_path(config, row['file_path'])
                    if not path.is_file():
                        error(422, 'invalid_slots', '选择的作品文件不存在')
                    style_name = _safe_filename(STYLE_BY_ID.get(row['style_id'], {}).get('name', row['style_id']), 'style')
                    source_task = _slot_source_task(conn, row) if row['kind'] == 'photo' else None
                    pose_id = source_task['pose_id'] if source_task else None
                    pose_name = (source_task['moment_title'] if source_task and source_task['moment_title'] else
                                 POSE_BY_ID.get(pose_id, {}).get('name') if pose_id else '样片')
                    pose_name = _safe_filename(pose_name, 'photo')
                    extension = path.suffix.lower() or '.png'
                    archive.write(path, f'{index:02d}_{style_name}_{pose_name}{extension}')
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
    filename = f"vowframe-{_safe_filename(project['name'], 'project')}.zip"
    encoded = quote(filename)
    headers = {'Content-Disposition': f'attachment; filename="vowframe-export.zip"; filename*=UTF-8\'\'{encoded}'}
    return FileResponse(temp_path, media_type='application/zip', headers=headers,
                        background=BackgroundTask(lambda: temp_path.unlink(missing_ok=True)))


@router.get('/projects/{project_id}/batches', response_model=list[Batch])
def list_batches(project_id: str, request: Request):
    with request.app.state.db.connect() as conn:
        get_project(conn, project_id)
        return [batch_response(conn, row) for row in conn.execute('SELECT * FROM generation_batches WHERE project_id=? ORDER BY created_at DESC,rowid DESC', (project_id,)).fetchall()]


@router.get('/batches/{batch_id}', response_model=Batch)
def batch_detail(batch_id: str, request: Request):
    with request.app.state.db.connect() as conn:
        return batch_response(conn, get_batch(conn, batch_id))


@router.post('/batches/{batch_id}/pause', response_model=Batch)
def pause_batch(batch_id: str, request: Request):
    """Pause a batch (V7.12): its queued tasks are held and are not sent until it continues; tasks already being
    generated finish, and the follow-ups they queue (a face fix or swap) are held too."""
    with request.app.state.db.connect(write=True) as conn:
        batch = get_batch(conn, batch_id)
        conn.execute('UPDATE generation_batches SET paused_at=? WHERE id=?', (utc_now(), batch_id))
        conn.execute("UPDATE generation_tasks SET status='held' WHERE batch_id=? AND status='waiting'", (batch_id,))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), batch['project_id']))
        return batch_response(conn, get_batch(conn, batch_id))


@router.post('/batches/{batch_id}/continue', response_model=Batch)
def continue_batch(batch_id: str, request: Request):
    """Queue a batch's held tasks again: paused by the user, or held after a restart or a failure."""
    with request.app.state.db.connect(write=True) as conn:
        batch = get_batch(conn, batch_id)
        conn.execute('UPDATE generation_batches SET paused_at=NULL WHERE id=?', (batch_id,))
        conn.execute("UPDATE generation_tasks SET status='waiting' WHERE batch_id=? AND status='held'", (batch_id,))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), batch['project_id']))
        result = batch_response(conn, get_batch(conn, batch_id))
    request.app.state.task_manager.notify()
    return result


# A task stopped while its request was out (V7.12): the provider cannot be told to stop, so the result is not kept.
STOPPED_MESSAGE = '已中断：请求已经发出，可能已经扣费；结果回来后不会保存'


@router.post('/batches/{batch_id}/stop', response_model=Batch)
def stop_batch(batch_id: str, request: Request):
    """Stop a batch now (V7.12): queued and held tasks are cancelled, and tasks being generated are stopped and
    their results discarded when they come back."""
    with request.app.state.db.connect(write=True) as conn:
        batch = get_batch(conn, batch_id)
        now = utc_now()
        conn.execute("UPDATE generation_tasks SET status='cancelled',finished_at=? WHERE batch_id=? AND status IN ('waiting','held')",
                     (now, batch_id))
        conn.execute("UPDATE generation_tasks SET status='cancelled',error_code='stopped',error_message=?,finished_at=? "
                     "WHERE batch_id=? AND status='running'", (STOPPED_MESSAGE, now, batch_id))
        conn.execute('UPDATE generation_batches SET paused_at=NULL WHERE id=?', (batch_id,))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, batch['project_id']))
        return batch_response(conn, get_batch(conn, batch_id))


@router.post('/batches/{batch_id}/cancel', response_model=Batch)
def cancel_batch(batch_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        batch = get_batch(conn, batch_id)
        conn.execute("UPDATE generation_tasks SET status='cancelled',finished_at=? WHERE batch_id=? AND status IN ('waiting','held')", (utc_now(), batch_id))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), batch['project_id']))
        return batch_response(conn, batch)


@router.get('/tasks', response_model=list[Task])
def list_tasks(request: Request, project_id: str | None = None, batch_id: str | None = None,
               status: TaskStatus | None = None, target_slot_id: str | None = None):
    filters = {key: value for key, value in {'project_id': project_id, 'batch_id': batch_id, 'status': status,
                                          'target_slot_id': target_slot_id}.items() if value is not None}
    query = 'SELECT * FROM generation_tasks'
    if filters:
        query += ' WHERE ' + ' AND '.join(f'{key}=?' for key in filters)
    query += ' ORDER BY created_at DESC,rowid DESC'
    with request.app.state.db.connect() as conn:
        return [task_response(row) for row in conn.execute(query, tuple(filters.values()))]


@router.post('/tasks/{task_id}/retry', response_model=Task, status_code=201)
def retry_task(task_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        original = get_task(conn, task_id)
        if conn.execute('SELECT 1 FROM photo_slots WHERE id=?', (original['target_slot_id'],)).fetchone() is None:
            error(409, 'slot_not_ready', '作品已删除，不能重新生成该任务')
        superseded = conn.execute(
            'SELECT 1 FROM generation_tasks WHERE retry_of_task_id=? LIMIT 1', (task_id,)
        ).fetchone()
        if superseded:
            error(409, 'task_already_retried', '该任务已经重试过，请查看最新的任务')
        if original['status'] not in ('failed', 'interrupted'):
            error(409, 'task_not_retryable', '只有失败或已中断的任务可以重试')
        new_task_id = new_id()
        status = 'held' if get_batch(conn, original['batch_id'])['paused_at'] else 'waiting'
        fields = ('project_id', 'batch_id', 'target_slot_id', 'reference_image_ids', 'style_id', 'pose_id', 'moment_title', 'moment_shot_size', 'prompt', 'model_config_snapshot', 'regenerate_mode', 'source_version_id')
        try:
            conn.execute('INSERT INTO generation_tasks (id,' + ','.join(fields) + ',status,retry_of_task_id,created_at) VALUES (' + ','.join('?' for _ in range(len(fields) + 4)) + ')',
                         (new_task_id, *(original[key] for key in fields), status, task_id, utc_now()))
        except sqlite3.IntegrityError:
            if conn.execute('SELECT 1 FROM generation_tasks WHERE retry_of_task_id=? LIMIT 1', (task_id,)).fetchone():
                error(409, 'task_already_retried', '该任务已经重试过，请查看最新的任务')
            raise
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), original['project_id']))
        result = task_response(get_task(conn, new_task_id))
    request.app.state.task_manager.notify()
    return result


@router.post('/tasks/{task_id}/cancel', response_model=Task)
def cancel_task(task_id: str, request: Request, expected_status: TaskStatus | None = None):
    """Cancel a queued or held task; since V7.12 also stop one being generated, whose result is then discarded."""
    with request.app.state.db.connect(write=True) as conn:
        task = get_task(conn, task_id)
        if expected_status is not None and task['status'] != expected_status:
            error(409, 'task_state_changed', '任务状态已改变，请刷新后重试；本次没有取消任何任务')
        if task['status'] not in ('waiting', 'held', 'running'):
            error(409, 'task_not_cancellable', '只有等待、已暂停或正在生成的任务可以取消')
        now = utc_now()
        conn.execute("UPDATE generation_tasks SET status='cancelled',finished_at=? WHERE id=? AND status IN ('waiting','held')", (now, task_id))
        conn.execute("UPDATE generation_tasks SET status='cancelled',error_code='stopped',error_message=?,finished_at=? "
                     "WHERE id=? AND status='running'", (STOPPED_MESSAGE, now, task_id))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, task['project_id']))
        return task_response(get_task(conn, task_id))


@router.post('/tasks/{task_id}/pause', response_model=Task)
def pause_task(task_id: str, request: Request):
    """Hold one queued task until it is continued (V7.12)."""
    with request.app.state.db.connect(write=True) as conn:
        task = get_task(conn, task_id)
        if task['status'] != 'waiting':
            error(409, 'task_not_pausable', '只有排队中的任务可以暂停；正在生成的请求已经发出，只能中断')
        conn.execute("UPDATE generation_tasks SET status='held' WHERE id=? AND status='waiting'", (task_id,))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), task['project_id']))
        return task_response(get_task(conn, task_id))


@router.post('/tasks/{task_id}/continue', response_model=Task)
def continue_task(task_id: str, request: Request):
    """Queue one held task again (V7.12)."""
    with request.app.state.db.connect(write=True) as conn:
        task = get_task(conn, task_id)
        if task['status'] != 'held':
            error(409, 'task_not_held', '只有已暂停的任务可以继续')
        if get_batch(conn, task['batch_id'])['paused_at']:
            error(409, 'batch_paused', '这批任务已暂停，请先继续整批任务')
        conn.execute("UPDATE generation_tasks SET status='waiting' WHERE id=? AND status='held'", (task_id,))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), task['project_id']))
        result = task_response(get_task(conn, task_id))
    request.app.state.task_manager.notify()
    return result
