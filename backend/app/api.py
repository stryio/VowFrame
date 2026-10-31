"""Project, asset and task-control HTTP endpoints."""

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
from .db import BATCH_REFERENCE_MODES, new_id, utc_now
from .face_swap import face_scores, face_swap_snapshot, face_swap_weight
from .models import MODEL_REGISTRY, get_model
from .prompts import DEFAULT_PREVIEW_STYLE_IDS, DETAIL_SHOT, HAIRSTYLES, HAIRSTYLE_BY_KEY, MAKEUP_COLORS, MAKEUP_COLOR_BY_ID, RETIRED_STYLE_IDS, POSES, POSE_BY_ID, STYLES, STYLE_BY_ID, STYLE_MAKEUP_COLORS, STYLING_VERSION, UPDO_STYLES, compose_identity_boost_moment_prompt, compose_look_prompt, compose_identity_boost_prompt, compose_moment_prompt, compose_photo_prompt, compose_preview_prompt, director_scene_count, director_scene_text, is_faceless_shot, scene_from_prompt, shot_text_from_prompt
from .director import DirectorFailed, DirectorInvalidOutput, Moment, SHOT_NAMES, SHOT_SIZES, _shot_requirement, moment_dict, validate_moment, validate_moments
from .schemas import Batch, BatchActionCreate, ChangePoseCreate, ChangeShotCreate, ExportCreate, FaceSwapCreate, Look, LookCreate, LookOptions, Model, MomentDraft, MomentDraftCreate, MomentPatch, MomentRewrite, PhotoBatchCreate, PhotoSet, PreviewCreate, ProjectCreate, ProjectDetail, ProjectPatch, ProjectSummary, RegenerateCreate, RestoreCreate, Settings, SettingsPatch, Slot, Style, Task, TaskStatus, VersionDetail
from .photo_prep import PhotoRejected
from .storage import asset_path, image_format, save_image
from .styling import (LOOK_PROBLEM_MESSAGES, MAX_SELECTED_LOOKS, default_assignment, default_look_items, fits_style, hair_length_of,
                      look_problem, look_response, look_snapshot, makeup_level_of, reference_slots, role_of,
                      task_face_swap_reference_ids, uploaded_asset_id)

router = APIRouter(prefix='/api')
logger = logging.getLogger(__name__)
STATUSES = ('waiting', 'held', 'running', 'completed', 'failed', 'cancelled', 'interrupted')
TASK_FIELDS = tuple(Task.model_fields)


def _uses_preview_reference(request: Request) -> bool:
    return request.app.state.db.get_batch_reference_mode() == 'with_preview'


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


def slot_response(conn, slot, project):
    current_ids = [project['person_a_image_id'], project['person_b_image_id']]
    if project['person_a_role'] == 'groom':
        current_ids.reverse()
    version = conn.execute(
        'SELECT version.id,version.image_asset_id AS asset_id,version.version_number,version.created_at,version.face_scores,'
        'task.model_config_snapshot,task.reference_image_ids,task.pose_id,task.moment_title,task.moment_shot_size,'
        'task.source_version_id FROM photo_versions version '
        'JOIN generation_tasks task ON task.id=version.generation_task_id WHERE version.id=?',
        (slot['current_version_id'],),
    ).fetchone() if slot['current_version_id'] else None
    stale = False
    styled_asset_ids = None
    look_ids = None
    if version is not None:
        try:
            used_references = json.loads(version['reference_image_ids'])
            # A styled photo stands in for the uploaded photo it was made from (V4.0).
            uploaded = [uploaded_asset_id(conn, asset_id) for asset_id in used_references]
            if uploaded[:2] != used_references[:2]:
                styled_asset_ids = {'bride': used_references[0], 'groom': used_references[1]}
            # A face swap records no looks of its own: they are the looks of the picture it started from.
            source = _slot_source_task(conn, slot)
            looks = json.loads(source['model_config_snapshot']).get('looks') if source is not None else None
            if isinstance(looks, dict) and looks.get('bride') and looks.get('groom'):
                look_ids = {'bride': looks['bride'], 'groom': looks['groom']}
            stale = uploaded[:2] != current_ids
            if not stale:
                side_ids = {project['person_a_left_image_id'], project['person_a_right_image_id'],
                            project['person_b_left_image_id'], project['person_b_right_image_id']}
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
        version_data = version_response({key: version[key] for key in ('id', 'asset_id', 'version_number', 'created_at', 'model_config_snapshot', 'source_version_id', 'face_scores')})
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
    previews = []
    slots = {row['style_id']: row for row in conn.execute("SELECT * FROM photo_slots WHERE project_id=? AND kind='preview'", (project['id'],))}
    for style in STYLES:
        slot = slots.get(style['id'])
        if slot is None:
            continue
        previews.append(slot_response(conn, slot, project))
    photos = [slot_response(conn, row, project) for row in _ordered_photo_slots(conn, project['id'])]
    photo_sets = []
    for row in conn.execute("SELECT b.style_id, MIN(b.created_at) AS first_created FROM generation_batches b WHERE b.project_id=? AND b.batch_type='photo' GROUP BY b.style_id ORDER BY first_created", (project['id'],)):
        style_id = row['style_id']
        slots_for_style = [p for p in photos if p['style_id'] == style_id]
        batch_ids = [r['id'] for r in conn.execute("SELECT id FROM generation_batches WHERE project_id=? AND batch_type='photo' AND style_id=? ORDER BY created_at,rowid", (project['id'], style_id))]
        photo_sets.append({'style_id': style_id, 'style_name': STYLE_BY_ID.get(style_id, {'name': style_id})['name'], 'slot_count': len(slots_for_style), 'completed_count': sum(p['current_version'] is not None for p in slots_for_style), 'batch_ids': batch_ids})
    result.update(makeup_level=project['makeup_level'], groom_makeup_level=project['groom_makeup_level'],
                  bride_hair_length=project['bride_hair_length'], groom_hair_length=project['groom_hair_length'],
                  person_a={'asset_id': project['person_a_image_id'], 'left_asset_id': project['person_a_left_image_id'], 'right_asset_id': project['person_a_right_image_id'], 'role': project['person_a_role']},
                  person_b={'asset_id': project['person_b_image_id'], 'left_asset_id': project['person_b_left_image_id'], 'right_asset_id': project['person_b_right_image_id'], 'role': 'groom' if project['person_a_role'] == 'bride' else 'bride'},
                  previews=previews, photo_sets=photo_sets, photos=photos,
                  looks=[look_response(project, row) for row in conn.execute(
                      "SELECT * FROM looks WHERE project_id=? AND angle='front' AND discarded_at IS NULL ORDER BY created_at,rowid",
                      (project['id'],))],
                  default_looks=default_assignment(conn, project),
                  default_look_items={role: [{'hairstyle_id': hairstyle, 'makeup_color': color}
                                             for hairstyle, color in default_look_items(project, role)]
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

    A face-swap version has no prompt of its own (it only replaces faces), so actions such as
    identity boost or change shot must start from the generation task the swap was applied to.
    """
    if slot['current_version_id']:
        task = _version_task(conn, slot['current_version_id'])
        while task is not None and task['regenerate_mode'] == 'face_swap' and task['source_version_id']:
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


def _slot_is_busy(conn, slot_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM generation_tasks task WHERE task.target_slot_id=? "
        "AND task.status IN ('waiting','running','held') AND NOT EXISTS ("
        'SELECT 1 FROM generation_tasks retry WHERE retry.retry_of_task_id=task.id) LIMIT 1',
        (slot_id,),
    ).fetchone() is not None


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
    people = {
        project['person_a_role']: ('a', project['person_a_image_id'], project['person_a_left_image_id'], project['person_a_right_image_id']),
        ('groom' if project['person_a_role'] == 'bride' else 'bride'): (
            'b', project['person_b_image_id'], project['person_b_left_image_id'], project['person_b_right_image_id']),
    }
    references = [people['bride'][1], people['groom'][1]]
    if looks is not None:
        references = [looks[0]['image_asset_id'], looks[1]['image_asset_id']]
    sides: list[tuple[str, str]] = []
    for role, facing in (('bride', bride_facing), ('groom', groom_facing)):
        if facing == 'front':
            continue
        _person, _front, left_id, right_id = people[role]
        asset_id = left_id if facing == 'left' else right_id
        # A missing optional side photo means only the front is sent.
        if asset_id:
            references.append(asset_id)
            sides.append((role, facing))
    if preview_asset_id:
        references.append(preview_asset_id)
    return references, sides


def _style_preview_reference(conn, project_id: str, style_id: str, slot_label: str) -> str:
    style_name = STYLE_BY_ID.get(style_id, {}).get('name', '该风格')
    preview_label = f'「{style_name}」样片'
    preview = conn.execute(
        "SELECT current_version_id FROM photo_slots WHERE project_id=? AND kind='preview' AND style_id=?",
        (project_id, style_id),
    ).fetchone()
    if preview is None or preview['current_version_id'] is None:
        error(409, 'slot_not_ready', f'{preview_label}尚未生成成功，无法作为{slot_label}的参考')
    version = conn.execute(
        'SELECT image_asset_id FROM photo_versions WHERE id=?', (preview['current_version_id'],),
    ).fetchone()
    if version is None:
        error(409, 'slot_not_ready', f'{preview_label}尚未生成成功，无法作为{slot_label}的参考')
    return version['image_asset_id']


def _validate_reference_capacity(model_id: str, references: list[str]):
    model = get_model(model_id)
    if model is None or len(references) > model.max_reference_images:
        invalid_model()


def _fresh_snapshot(request: Request, model_id: str, aspect_ratio: str, looks=None,
                    side_references: list[tuple[str, str]] = (), with_preview: bool = False) -> str:
    """looks: the (bride, groom) look rows of a shot (V5.0), recorded with what each reference image is."""
    snapshot = dict(request.app.state.config.model_config_snapshot)
    snapshot['model'] = model_id
    snapshot['aspect_ratio'] = aspect_ratio
    if looks is not None:
        snapshot['looks'] = look_snapshot(looks[0]['id'], looks[1]['id'])
        snapshot['reference_slots'] = reference_slots(list(side_references), with_preview)
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


def _source_looks(conn, project, source, slot_label: str):
    """The looks of the picture a slot action starts from; a new shot keeps them (V5.0)."""
    looks = _parse_snapshot(source['model_config_snapshot'], slot_label).get('looks')
    if not isinstance(looks, dict) or not looks.get('bride') or not looks.get('groom'):
        error(409, 'looks_required', f'{slot_label}来自旧版造型流程，没有记录造型；请先在「造型」一步选好造型，再重新生成这个风格的样片')
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


def _director_write(request: Request, *, style: str, scene: str, scene_count: int, count: int, shot_requirement: str,
                    avoid_titles: list[str]) -> list[Moment]:
    director = _director(request)
    try:
        first = _director_call(request, lambda value: value.write_moments(style, scene, count, shot_requirement, avoid_titles,
                                                                          scene_count=scene_count))
        problems = validate_moments(first, count=count, avoid_titles=avoid_titles, scene_count=scene_count)
        if problems:
            feedback = '上面的输出不符合要求：' + '；'.join(problems) + '。请修正后重新输出完整 JSON。'
            second = _director_call(request, lambda value: value.write_moments(style, scene + '\n\n' + feedback, count, shot_requirement,
                                                                               avoid_titles, scene_count=scene_count))
            problems = validate_moments(second, count=count, avoid_titles=avoid_titles, scene_count=scene_count)
            if problems:
                raise DirectorInvalidOutput(problems)
            return second
        return first
    except DirectorInvalidOutput:
        error(502, 'director_invalid_output', 'AI 导演没有写出符合要求的方案，请重试')


def _director_rewrite_shot(request: Request, *, style: str, scene: str, original: str, target: str,
                           orientation: str) -> Moment:
    try:
        first = _director_call(request, lambda value: value.rewrite_shot(style, scene, original, target, orientation))
        problems = validate_moment(first, target_shot=target, target_orientation=orientation, check_facing_text=True)
        if problems:
            feedback = '上面的输出不符合要求：' + '；'.join(problems) + '。请修正后重新输出完整 JSON。'
            second = _director_call(request, lambda value: value.rewrite_shot(style, scene + '\n\n' + feedback, original, target, orientation))
            problems = validate_moment(second, target_shot=target, target_orientation=orientation, check_facing_text=True)
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
    references, side_references = _build_references(project, looks=looks)
    batch_reference_mode = request.app.state.db.get_batch_reference_mode()
    with_preview = slot['kind'] == 'photo' and batch_reference_mode == 'with_preview'
    if with_preview:
        references.append(_style_preview_reference(conn, project['id'], style_id, slot_label))

    aspect_ratio = _source_aspect_ratio(source, request)
    if action == 'identity_boost':
        if source['moment_shot_size'] == DETAIL_SHOT:
            error(409, 'no_faces', f'{slot_label}是局部特写，画面里没有人脸，无需加强长相')
        pose_id = source['pose_id']
        prompt = (compose_identity_boost_moment_prompt(style_id, shot_text_from_prompt(source['prompt']), aspect_ratio,
                                                       scene=scene_from_prompt(style_id, source['prompt']), with_preview=with_preview,
                                                       styled=True)
                  if source['moment_shot_size'] else compose_identity_boost_prompt(style_id, pose_id, aspect_ratio, with_preview=with_preview,
                                                                                   styled=True))
        regenerate_mode = 'identity_boost'
    elif action == 'change_pose':
        if slot['kind'] != 'photo':
            error(409, 'slot_not_ready', f'{slot_label}不是相册作品，不能更换动作')
        if source['moment_title'] is None and selected_pose is not None:
            pose_id = selected_pose
            prompt = compose_photo_prompt(style_id, pose_id, aspect_ratio, with_preview=with_preview, styled=True)
            regenerate_mode = 'change_pose'
            moment_title, moment_shot_size = None, None
        else:
            style = STYLE_BY_ID[style_id]
            scene = director_scene_text(style_id, single_variant=with_preview)
            avoid = [row['moment_title'] for row in conn.execute(
                'SELECT DISTINCT moment_title FROM generation_tasks WHERE project_id=? AND style_id=? AND moment_title IS NOT NULL',
                (project['id'], style_id)) if row['moment_title']]
            avoid.extend(avoid_extra or [])
            moment = _director_write(request, style=style['name'], scene=scene,
                                     scene_count=director_scene_count(style_id, single_variant=with_preview), count=1,
                                     shot_requirement=_shot_requirement(1), avoid_titles=avoid)[0]
            pose_id = None
            # A new shot gets the frame the director chose for it.
            aspect_ratio = _shot_ratio(request, moment.orientation)
            references, side_references = _build_references(project, moment.bride_facing, moment.groom_facing,
                                                               _style_preview_reference(conn, project['id'], style_id, slot_label) if with_preview else None,
                                                               looks=looks)
            prompt = compose_moment_prompt(style_id, moment.text, aspect_ratio, scene=moment.scene, faces_clear=moment.faces_clear,
                                           with_preview=with_preview, side_references=side_references, styled=True)
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
        scene = director_scene_text(style_id, single_variant=with_preview)
        action_text = shot_text_from_prompt(source['prompt'])
        moment = _director_rewrite_shot(request, style=style['name'], scene=scene, original=action_text,
                                        target=target_shot, orientation=orientation_of(aspect_ratio))
        pose_id = None
        references, side_references = _build_references(project, moment.bride_facing, moment.groom_facing,
                                                           _style_preview_reference(conn, project['id'], style_id, slot_label) if with_preview else None,
                                                           looks=looks)
        # Changing the shot size keeps the scene and whether it is an atmosphere shot.
        prompt = compose_moment_prompt(style_id, moment.text, aspect_ratio, scene=scene_from_prompt(style_id, source['prompt']),
                                       faces_clear=not is_faceless_shot(source['prompt']),
                                       with_preview=with_preview, side_references=side_references, styled=True)
        regenerate_mode = 'change_shot'
        moment_title, moment_shot_size = moment.title, moment.shot_size
    else:
        raise ValueError(f'Unsupported slot action: {action}')

    model_id = explicit_model or request.app.state.db.get_default_model()
    _validate_reference_capacity(model_id, references)
    return {
        'slot_id': slot_id, 'style_id': style_id, 'pose_id': pose_id,
        'moment_title': locals().get('moment_title', source['moment_title']),
        'moment_shot_size': locals().get('moment_shot_size', source['moment_shot_size']),
        'references': json.dumps(references, ensure_ascii=False), 'prompt': prompt,
        'snapshot': _fresh_snapshot(request, model_id, aspect_ratio, looks, side_references, with_preview),
        'regenerate_mode': regenerate_mode,
    }


def _prepare_face_swap_spec(conn, slot, project, request: Request, *, slot_label: str) -> dict:
    swap_status = request.app.state.face_swapper.status
    if not swap_status['available']:
        error(409, 'face_swap_unavailable', swap_status['reason'])
    if slot['current_version_id'] is None:
        error(409, 'slot_not_ready', f'{slot_label}尚无成功版本，暂时无法换脸修正')
    if _slot_is_busy(conn, slot['id']):
        error(409, 'slot_busy', f'{slot_label}已有任务正在排队或生成，请等它完成后再操作')
    if not project['person_a_image_id'] or not project['person_b_image_id']:
        error(409, 'photos_missing', '请先上传两个人的照片')

    current = conn.execute(
        'SELECT version.id AS version_id,task.* FROM photo_versions version '
        'JOIN generation_tasks task ON task.id=version.generation_task_id '
        'WHERE version.id=? AND version.photo_slot_id=?',
        (slot['current_version_id'], slot['id']),
    ).fetchone()
    if current is None:
        error(409, 'slot_not_ready', f'{slot_label}的当前版本记录不存在，暂时无法换脸修正')
    input_version_id = current['version_id']
    input_task = current
    if current['regenerate_mode'] == 'face_swap':
        input_version_id = current['source_version_id']
        if input_version_id is None:
            error(409, 'slot_not_ready', f'{slot_label}的来源版本不存在，暂时无法换脸修正')
        input_task = conn.execute(
            'SELECT version.id AS version_id,task.* FROM photo_versions version '
            'JOIN generation_tasks task ON task.id=version.generation_task_id '
            'WHERE version.id=? AND version.photo_slot_id=?',
            (input_version_id, slot['id']),
        ).fetchone()
        if input_task is None:
            error(409, 'slot_not_ready', f'{slot_label}的来源版本不存在，暂时无法换脸修正')

    if input_task['moment_shot_size'] == DETAIL_SHOT:
        error(409, 'no_faces', f'{slot_label}是局部特写，画面里没有人脸，无需换脸')
    # The swap uses the faces of the shot it fixes: its looks or styled photos (V4.0, V5.0).
    references = task_face_swap_reference_ids(conn, project, _parse_snapshot(input_task['model_config_snapshot'], slot_label),
                                              _parse_json_list(input_task['reference_image_ids'], slot_label))
    mode = swap_status['mode']
    snapshot = face_swap_snapshot(mode, face_swap_weight(slot['kind']))
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
        'source_version_id': input_version_id,
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
def models():
    return [model.__dict__ for model in MODEL_REGISTRY]


def _settings_response(request: Request) -> dict:
    db = request.app.state.db
    return {'default_model': db.get_default_model(), 'batch_reference_mode': db.get_batch_reference_mode(),
            'director_model': db.get_director_model(), 'portrait_ratio': db.get_portrait_ratio(),
            'auto_face_swap': db.get_auto_face_swap()}


@router.get('/settings', response_model=Settings)
def get_settings(request: Request):
    return _settings_response(request)


@router.patch('/settings', response_model=Settings)
def patch_settings(body: SettingsPatch, request: Request):
    changes = body.model_dump(exclude_unset=True)
    model = changes.get('default_model')
    if model is not None:
        if get_model(model) is None:
            invalid_model()
        request.app.state.db.set_default_model(model)
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
    if changes.get('auto_face_swap') is not None:
        request.app.state.db.set_auto_face_swap(changes['auto_face_swap'])
    if 'director_model' in changes:
        director_model = changes['director_model']
        if director_model == '':
            error(422, 'invalid_director_model', 'AI 导演模型不能为空字符串')
        request.app.state.db.set_director_model(director_model)
        if hasattr(request.app.state.director, 'model'):
            request.app.state.director.model = director_model
    return _settings_response(request)


@router.get('/styles', response_model=list[Style])
def styles():
    return [{**style, 'default_preview': style['id'] in DEFAULT_PREVIEW_STYLE_IDS, 'retired': style['id'] in RETIRED_STYLE_IDS}
            for style in STYLES]


@router.get('/look-options', response_model=LookOptions)
def look_options():
    return {
        'hairstyles': [{'id': item.id, 'name': item.name, 'role': item.role, 'lengths': list(item.lengths), 'updo': item.updo}
                       for item in HAIRSTYLES],
        'makeup_colors': [item._asdict() for item in MAKEUP_COLORS],
        'styles': {style_id: {'primary': primary, 'alternates': list(alternates), 'needs_updo': style_id in UPDO_STYLES}
                   for style_id, (primary, alternates) in STYLE_MAKEUP_COLORS.items()},
        'max_selected': MAX_SELECTED_LOOKS,
    }


def _get_look(conn, look_id: str):
    look = conn.execute("SELECT * FROM looks WHERE id=? AND angle='front' AND discarded_at IS NULL", (look_id,)).fetchone()
    if look is None:
        error(404, 'look_not_found', '造型不存在')
    return look


@router.post('/projects/{project_id}/looks', response_model=list[Look], status_code=201)
def create_looks(project_id: str, body: LookCreate, request: Request, response: Response):
    """Look candidates for one person (V5.0): each item is one paid edit of their uploaded photo."""
    with request.app.state.db.connect(write=True) as conn:
        project = get_project(conn, project_id)
        existing = conn.execute('SELECT * FROM looks WHERE client_request_id=? ORDER BY rowid', (body.client_request_id,)).fetchall()
        if existing:
            if any(row['project_id'] != project_id or row['person'] != body.person for row in existing):
                error(409, 'request_id_reused', '这个请求编号已经用于另一组造型，请刷新后重试')
            response.status_code = 200
            return [look_response(project, row) for row in existing]
        source = project[f'person_{body.person}_image_id']
        if not source:
            error(409, 'photos_missing', '请先上传这个人的照片')
        role = role_of(project, body.person)
        length, level = hair_length_of(project, role), makeup_level_of(project, role)
        model_id = request.app.state.db.get_default_model()
        now, ids = utc_now(), []
        for item in body.items:
            hairstyle = HAIRSTYLE_BY_KEY.get((role, item.hairstyle_id))
            if hairstyle is None or length not in hairstyle.lengths:
                error(422, 'invalid_hairstyle', '这个发型不适合当前的头发长度，请换一个')
            if role == 'bride' and item.makeup_color not in MAKEUP_COLOR_BY_ID:
                error(422, 'invalid_makeup_color', '请为新娘选择一种妆色')
            if role == 'groom' and item.makeup_color is not None:
                error(422, 'invalid_makeup_color', '新郎不选妆色')
            look_id = new_id()
            conn.execute(
                'INSERT INTO looks (id,project_id,person,role,angle,hairstyle_id,hair_length,makeup_color,makeup_level,'
                "source_asset_id,styling_version,status,model,prompt,client_request_id,created_at) VALUES (?,?,?,?,'front',?,?,?,?,?,?,'waiting',?,?,?,?)",
                (look_id, project_id, body.person, role, item.hairstyle_id, length, item.makeup_color, level, source,
                 STYLING_VERSION, model_id, compose_look_prompt(role, item.hairstyle_id, level, item.makeup_color),
                 body.client_request_id, now))
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
        conn.execute("UPDATE looks SET status='waiting',error_code=NULL,error_message=NULL,finished_at=NULL WHERE id=?", (look_id,))
        result = look_response(project, _get_look(conn, look_id))
    request.app.state.task_manager.notify()
    return result


@router.post('/looks/{look_id}/select', response_model=Look)
def select_look(look_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        look = _get_look(conn, look_id)
        project = get_project(conn, look['project_id'])
        problem = look_problem(project, look)
        if problem is not None:
            error(409, 'look_unusable', LOOK_PROBLEM_MESSAGES[problem])
        if look['selected_order'] is None:
            selected = [row for row in conn.execute(
                "SELECT * FROM looks WHERE project_id=? AND person=? AND angle='front' AND selected_order IS NOT NULL "
                'AND discarded_at IS NULL', (project['id'], look['person'])) if look_problem(project, row) is None]
            if len(selected) >= MAX_SELECTED_LOOKS:
                error(409, 'too_many_looks', f'每人最多选用 {MAX_SELECTED_LOOKS} 套造型，请先取消一套')
            top = conn.execute('SELECT MAX(selected_order) FROM looks WHERE project_id=? AND person=?',
                               (project['id'], look['person'])).fetchone()[0]
            conn.execute('UPDATE looks SET selected_order=? WHERE id=?', ((top or 0) + 1, look_id))
        return look_response(project, _get_look(conn, look_id))


@router.post('/looks/{look_id}/unselect', response_model=Look)
def unselect_look(look_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        look = _get_look(conn, look_id)
        conn.execute('UPDATE looks SET selected_order=NULL WHERE id=?', (look_id,))
        return look_response(get_project(conn, look['project_id']), _get_look(conn, look_id))


@router.post('/looks/{look_id}/make-default', response_model=Look)
def make_default_look(look_id: str, request: Request):
    """Move a selected look to the front of its person's order, so styles use it first."""
    with request.app.state.db.connect(write=True) as conn:
        look = _get_look(conn, look_id)
        if look['selected_order'] is None:
            error(409, 'look_not_selected', '请先选用这套造型')
        bottom = conn.execute('SELECT MIN(selected_order) FROM looks WHERE project_id=? AND person=?',
                              (look['project_id'], look['person'])).fetchone()[0]
        conn.execute('UPDATE looks SET selected_order=? WHERE id=?', (bottom - 1, look_id))
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
            changes['updated_at'] = utc_now()
            conn.execute('UPDATE projects SET ' + ','.join(f'{key}=?' for key in changes) + ' WHERE id=?', (*changes.values(), project_id))
        return project_response(conn, get_project(conn, project_id))


@router.delete('/projects/{project_id}', status_code=204)
def delete_project(project_id: str, request: Request):
    db, config = request.app.state.db, request.app.state.config
    folder = None
    with db.connect(write=True) as conn:
        get_project(conn, project_id)
        conn.execute("UPDATE generation_tasks SET status='cancelled',finished_at=? WHERE project_id=? AND status IN ('waiting','held')", (utc_now(), project_id))
        # Queued look candidates have not been sent yet; one being edited is waited for like a running shot.
        conn.execute("UPDATE looks SET status='failed',error_code='cancelled',error_message='项目已删除',finished_at=? "
                     "WHERE project_id=? AND status='waiting'", (utc_now(), project_id))
        busy = (conn.execute("SELECT 1 FROM generation_tasks WHERE project_id=? AND status='running' LIMIT 1", (project_id,)).fetchone() is not None
                or conn.execute("SELECT 1 FROM looks WHERE project_id=? AND status='running' LIMIT 1", (project_id,)).fetchone() is not None)
        if not busy:
            # Break cyclic references before cascading removal of project-owned rows.
            conn.execute('UPDATE projects SET person_a_image_id=NULL,person_a_left_image_id=NULL,person_a_right_image_id=NULL,person_b_image_id=NULL,person_b_left_image_id=NULL,person_b_right_image_id=NULL,selected_preview_version_id=NULL WHERE id=?', (project_id,))
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
                 angle: Literal['front', 'left', 'right'] = Form('front'), file: UploadFile = File(...)):
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
        column = f'person_{person}_image_id' if angle == 'front' else f'person_{person}_{angle}_image_id'
        conn.execute(f'UPDATE projects SET {column}=?,updated_at=? WHERE id=?', (asset_id, now, project_id))
        return project_response(conn, get_project(conn, project_id))


@router.delete('/projects/{project_id}/images/{person}/{angle}', status_code=204)
def delete_side_image(project_id: str, person: Literal['a', 'b'], angle: Literal['left', 'right'], request: Request):
    with request.app.state.db.connect(write=True) as conn:
        get_project(conn, project_id)
        column = f'person_{person}_{angle}_image_id'
        conn.execute(f'UPDATE projects SET {column}=NULL,updated_at=? WHERE id=?', (utc_now(), project_id))
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
        if model is None:
            invalid_model()
        style_ids = body.style_ids if body.style_ids is not None else list(DEFAULT_PREVIEW_STYLE_IDS)
        if not style_ids or len(set(style_ids)) != len(style_ids) or any(style not in STYLE_BY_ID for style in style_ids):
            error(422, 'invalid_styles', '请选择有效且不重复的风格')
        # A retired style may only refresh a sample this project already has.
        retired = [style for style in style_ids if style in RETIRED_STYLE_IDS and conn.execute(
            "SELECT 1 FROM photo_slots WHERE project_id=? AND kind='preview' AND style_id=?", (project_id, style)).fetchone() is None]
        if retired:
            error(422, 'invalid_styles', '「' + '」「'.join(STYLE_BY_ID[style]['name'] for style in retired) + '」已停用，不能生成新的样片')
        if body.looks is None or set(body.looks) != set(style_ids):
            error(422, 'invalid_looks', '请为每个风格选好新娘和新郎的造型')
        style_looks = {}
        for style_id in style_ids:
            label = f'「{STYLE_BY_ID[style_id]["name"]}」'
            pair = (_usable_look(conn, project, body.looks[style_id].bride, 'bride', label),
                    _usable_look(conn, project, body.looks[style_id].groom, 'groom', label))
            for look in pair:
                if look['selected_order'] is None:
                    error(409, 'look_unusable', f'{label}：只能使用已选用的造型')
            if not fits_style(pair[0], style_id):
                error(409, 'look_unfit', f'{label}需要盘发造型，请为新娘换一套盘发造型')
            style_looks[style_id] = pair
        references, _ = _build_references(project)
        if len(references) > model.max_reference_images:
            error(422, 'invalid_model', '所选模型支持的参考照片数量不足')
        batch_id, now = new_id(), utc_now()
        conn.execute('INSERT INTO generation_batches (id,project_id,batch_type,client_request_id,created_at) VALUES (?,?,?,?,?)', (batch_id, project_id, 'preview', body.client_request_id, now))
        # Samples are always portrait; the director decides frames only for photo sets.
        preview_ratio = _shot_ratio(request, 'portrait')
        for style_id in style_ids:
            looks = style_looks[style_id]
            references, _ = _build_references(project, looks=looks)
            snapshot = _fresh_snapshot(request, model_id, preview_ratio, looks)
            slot = conn.execute("SELECT id FROM photo_slots WHERE project_id=? AND kind='preview' AND style_id=?", (project_id, style_id)).fetchone()
            slot_id = slot['id'] if slot else new_id()
            if slot is None:
                conn.execute('INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,created_at) VALUES (?,?,?,?,?,?)', (slot_id, project_id, batch_id, 'preview', style_id, now))
            conn.execute(
                'INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,prompt,model_config_snapshot,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (new_id(), project_id, batch_id, slot_id, 'waiting', json.dumps(references), style_id,
                 compose_preview_prompt(style_id, preview_ratio, styled=True), snapshot, utc_now()),
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
        # With the preview as a reference image, every shot stays in the preview's scene (docs/prompts.md 7.2).
        single_variant = _uses_preview_reference(request)
        scene = director_scene_text(preview['style_id'], single_variant=single_variant)
        result = _director_write(request, style=style['name'], scene=scene,
                                 scene_count=director_scene_count(preview['style_id'], single_variant=single_variant), count=count,
                                 shot_requirement=_shot_requirement(count), avoid_titles=avoid)
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
        single_variant = _uses_preview_reference(request)
        scene = director_scene_text(row['style_id'], single_variant=single_variant)
        current = moments[index]
        avoid = [item.title for item in moments]
        if body.shot_size:
            replacement = _director_rewrite_shot(request, style=style['name'], scene=scene, original=current.text,
                                                 target=body.shot_size, orientation=current.orientation)
            # Changing the shot size keeps the scene and whether it is an atmosphere shot.
            replacement = replace(replacement, scene=current.scene, faces_clear=current.faces_clear)
        else:
            replacement = _director_write(request, style=style['name'], scene=scene,
                                          scene_count=director_scene_count(row['style_id'], single_variant=single_variant), count=1,
                                          shot_requirement=f'保持 {SHOT_NAMES[current.shot_size]}。',
                                          avoid_titles=avoid)[0]
        problems = validate_moment(replacement, avoid_titles=[item.title for item in moments if item is not current], target_shot=body.shot_size,
                                   target_orientation=current.orientation if body.shot_size else None, check_facing_text=True)
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
        if model is None:
            invalid_model()
        mode = request.app.state.db.get_batch_reference_mode()
        preview_version = preview['current_version_id']
        # A photo set is shot in its sample's looks (V5.0).
        looks = _source_looks(conn, project, _slot_source_task(conn, preview), f'「{STYLE_BY_ID[preview["style_id"]]["name"]}」样片')
        preview_asset = None
        if mode == 'with_preview':
            preview_asset = conn.execute('SELECT image_asset_id FROM photo_versions WHERE id=?', (preview_version,)).fetchone()['image_asset_id']
        prepared_references = []
        offending_shots = []
        for moment in moments:
            references, side_references = _build_references(project, moment.bride_facing, moment.groom_facing, preview_asset, looks=looks)
            prepared_references.append((references, side_references))
            if len(references) > model.max_reference_images:
                offending_shots.append(moment.title)
        if offending_shots:
            required = max(len(references) for references, _ in prepared_references)
            error(422, 'invalid_model', f'以下镜头需要 {required} 张参考图，超过所选模型上限：{"、".join(offending_shots)}。请切换模型或关闭「带样片参考」')
        batch_id, now = new_id(), utc_now()
        conn.execute('INSERT INTO generation_batches (id,project_id,batch_type,client_request_id,created_at,style_id,reference_preview_version_id) VALUES (?,?,?,?,?,?,?)', (batch_id, project_id, 'photo', body.client_request_id, now, preview['style_id'], preview_version))
        for moment, (references, side_references) in zip(moments, prepared_references):
            aspect_ratio = _shot_ratio(request, moment.orientation)
            snapshot = _fresh_snapshot(request, model_id, aspect_ratio, looks, side_references, mode == 'with_preview')
            slot_id, task_id = new_id(), new_id()
            conn.execute('INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,pose_id,pose_name,shot_size,created_at) VALUES (?,?,?,?,?,?,?,?,?)', (slot_id, project_id, batch_id, 'photo', preview['style_id'], None, moment.title, moment.shot_size, utc_now()))
            prompt = compose_moment_prompt(preview['style_id'], moment.text, aspect_ratio, scene=moment.scene,
                                           faces_clear=moment.faces_clear, with_preview=mode == 'with_preview',
                                           side_references=side_references, styled=True)
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
            'SELECT version.id,version.image_asset_id AS asset_id,version.version_number,version.created_at,version.face_scores,'
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


@router.post('/batches/{batch_id}/continue', response_model=Batch)
def continue_batch(batch_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        batch = get_batch(conn, batch_id)
        conn.execute("UPDATE generation_tasks SET status='waiting' WHERE batch_id=? AND status='held'", (batch_id,))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), batch['project_id']))
        result = batch_response(conn, batch)
    request.app.state.task_manager.notify()
    return result


@router.post('/batches/{batch_id}/cancel', response_model=Batch)
def cancel_batch(batch_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        batch = get_batch(conn, batch_id)
        conn.execute("UPDATE generation_tasks SET status='cancelled',finished_at=? WHERE batch_id=? AND status IN ('waiting','held')", (utc_now(), batch_id))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), batch['project_id']))
        return batch_response(conn, batch)


@router.get('/tasks', response_model=list[Task])
def list_tasks(request: Request, project_id: str | None = None, batch_id: str | None = None, status: TaskStatus | None = None):
    filters = {key: value for key, value in {'project_id': project_id, 'batch_id': batch_id, 'status': status}.items() if value is not None}
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
        fields = ('project_id', 'batch_id', 'target_slot_id', 'reference_image_ids', 'style_id', 'pose_id', 'moment_title', 'moment_shot_size', 'prompt', 'model_config_snapshot', 'regenerate_mode', 'source_version_id')
        try:
            conn.execute('INSERT INTO generation_tasks (id,' + ','.join(fields) + ',status,retry_of_task_id,created_at) VALUES (' + ','.join('?' for _ in range(len(fields) + 4)) + ')',
                         (new_task_id, *(original[key] for key in fields), 'waiting', task_id, utc_now()))
        except sqlite3.IntegrityError:
            if conn.execute('SELECT 1 FROM generation_tasks WHERE retry_of_task_id=? LIMIT 1', (task_id,)).fetchone():
                error(409, 'task_already_retried', '该任务已经重试过，请查看最新的任务')
            raise
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), original['project_id']))
        result = task_response(get_task(conn, new_task_id))
    request.app.state.task_manager.notify()
    return result


@router.post('/tasks/{task_id}/cancel', response_model=Task)
def cancel_task(task_id: str, request: Request):
    with request.app.state.db.connect(write=True) as conn:
        task = get_task(conn, task_id)
        if task['status'] not in ('waiting', 'held'):
            error(409, 'task_not_cancellable', '只有等待或已搁置的任务可以取消')
        conn.execute("UPDATE generation_tasks SET status='cancelled',finished_at=? WHERE id=? AND status IN ('waiting','held')", (utc_now(), task_id))
        conn.execute('UPDATE projects SET updated_at=? WHERE id=?', (utc_now(), task['project_id']))
        return task_response(get_task(conn, task_id))
