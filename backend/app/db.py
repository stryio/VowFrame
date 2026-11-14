"""SQLite persistence; every operation owns its connection and transaction."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .aspect import DEFAULT_PORTRAIT_RATIO, PORTRAIT_RATIOS
from .models import DEFAULT_MODEL, DIRECTOR_PROVIDERS, MODELS_BY_ID, PROVIDERS, provider_models


def new_id() -> str:
    return uuid4().hex


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Kept apart from SCHEMA so the V7.22 upgrade can rebuild the table with the same definition.
MAKEUPS_TABLE = """CREATE TABLE IF NOT EXISTS {name} (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    person TEXT NOT NULL CHECK(person IN ('a','b')), role TEXT NOT NULL CHECK(role IN ('bride','groom')), style_id TEXT,
    makeup_color TEXT, makeup_level TEXT NOT NULL,
    retouch_settings TEXT CHECK(retouch_settings IS NULL OR json_valid(retouch_settings)),
    source_asset_id TEXT NOT NULL REFERENCES image_assets(id), styling_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting','running','ready','failed','interrupted')),
    error_code TEXT, error_message TEXT, model TEXT, prompt TEXT NOT NULL,
    image_asset_id TEXT REFERENCES image_assets(id), face_score REAL, selected_by TEXT,
    client_request_id TEXT, created_at TEXT NOT NULL, finished_at TEXT, discarded_at TEXT, makeup_style_id TEXT
);
"""
MAKEUPS_INDEXES = """CREATE INDEX IF NOT EXISTS makeup_project ON makeups(project_id,person,style_id);
CREATE INDEX IF NOT EXISTS makeup_queue ON makeups(status,created_at);
CREATE INDEX IF NOT EXISTS makeup_request ON makeups(client_request_id);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    person_a_image_id TEXT REFERENCES image_assets(id),
    person_a_left_image_id TEXT REFERENCES image_assets(id),
    person_a_right_image_id TEXT REFERENCES image_assets(id),
    person_a_left_full_image_id TEXT REFERENCES image_assets(id),
    person_a_right_full_image_id TEXT REFERENCES image_assets(id),
    person_a_role TEXT NOT NULL DEFAULT 'bride' CHECK(person_a_role IN ('bride','groom')),
    person_b_image_id TEXT REFERENCES image_assets(id),
    person_b_left_image_id TEXT REFERENCES image_assets(id),
    person_b_right_image_id TEXT REFERENCES image_assets(id),
    person_b_left_full_image_id TEXT REFERENCES image_assets(id),
    person_b_right_full_image_id TEXT REFERENCES image_assets(id),
    selected_style_id TEXT REFERENCES style_presets(id),
    selected_preview_version_id TEXT REFERENCES photo_versions(id),
    makeup_level TEXT NOT NULL DEFAULT 'natural' CHECK(makeup_level IN ('light','natural','refined')),
    groom_makeup_level TEXT NOT NULL DEFAULT 'natural' CHECK(groom_makeup_level IN ('light','natural','refined')),
    bride_retouch_settings TEXT CHECK(bride_retouch_settings IS NULL OR json_valid(bride_retouch_settings)),
    groom_retouch_settings TEXT CHECK(groom_retouch_settings IS NULL OR json_valid(groom_retouch_settings)),
    bride_hair_length TEXT NOT NULL DEFAULT 'long' CHECK(bride_hair_length IN ('long','medium','short')),
    groom_hair_length TEXT NOT NULL DEFAULT 'short' CHECK(groom_hair_length IN ('buzz','short','medium')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS image_assets (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL, asset_type TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS style_presets (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
    prompt_template TEXT NOT NULL, is_builtin INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS generation_batches (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    batch_type TEXT NOT NULL CHECK(batch_type IN ('preview','photo','regenerate')),
    client_request_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
    style_id TEXT REFERENCES style_presets(id),
    reference_preview_version_id TEXT REFERENCES photo_versions(id),
    -- V7.12: set while the user has paused the batch; its follow-up tasks are queued held too.
    paused_at TEXT
);
CREATE TABLE IF NOT EXISTS photo_slots (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES generation_batches(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('preview','photo')),
    style_id TEXT NOT NULL REFERENCES style_presets(id),
    current_version_id TEXT REFERENCES photo_versions(id), created_at TEXT NOT NULL,
    pose_id TEXT, pose_name TEXT, shot_size TEXT,
    -- V7.56: the room (scene) a sample is shot in; a project has one sample per style and room (NULL: no room).
    -- Its unique index is made in Database.initialize, after older databases have the column.
    room_id TEXT
);
CREATE TABLE IF NOT EXISTS generation_tasks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES generation_batches(id) ON DELETE CASCADE,
    -- Retain this historical identifier after a slot is deleted so task history remains readable.
    target_slot_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting','held','running','completed','failed','cancelled','interrupted')),
    reference_image_ids TEXT NOT NULL CHECK(json_valid(reference_image_ids)),
    style_id TEXT NOT NULL REFERENCES style_presets(id), pose_id TEXT,
    moment_title TEXT, moment_shot_size TEXT,
    prompt TEXT NOT NULL, model_config_snapshot TEXT NOT NULL CHECK(json_valid(model_config_snapshot)),
    provider_task_id TEXT, output_asset_id TEXT REFERENCES image_assets(id),
    error_code TEXT, error_message TEXT,
    auto_retry_count INTEGER NOT NULL DEFAULT 0 CHECK(auto_retry_count BETWEEN 0 AND 2),
    retry_of_task_id TEXT REFERENCES generation_tasks(id),
    regenerate_mode TEXT CHECK(regenerate_mode IS NULL OR regenerate_mode IN ('same','identity_boost','change_pose','change_shot','face_swap','face_fix','hand_fix','wash','regional_edit')),
    source_version_id TEXT,
    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS task_queue ON generation_tasks(status,created_at);
CREATE INDEX IF NOT EXISTS task_batch ON generation_tasks(batch_id);
CREATE INDEX IF NOT EXISTS task_project ON generation_tasks(project_id);
CREATE INDEX IF NOT EXISTS task_slot ON generation_tasks(target_slot_id,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS task_retry_once
    ON generation_tasks(retry_of_task_id) WHERE retry_of_task_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS photo_versions (
    id TEXT PRIMARY KEY, photo_slot_id TEXT NOT NULL REFERENCES photo_slots(id) ON DELETE CASCADE,
    image_asset_id TEXT NOT NULL REFERENCES image_assets(id),
    generation_task_id TEXT NOT NULL UNIQUE REFERENCES generation_tasks(id),
    version_number INTEGER NOT NULL CHECK(version_number>0), created_at TEXT NOT NULL,
    UNIQUE(photo_slot_id,version_number)
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- V4.0: one person's photo edited into their styled photo (hairstyle, skin, makeup, plain background) for a style.
-- The key is everything the edit depends on; restyling marks the rows discarded so the next shot edits again.
CREATE TABLE IF NOT EXISTS styled_photos (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    style_id TEXT NOT NULL REFERENCES style_presets(id),
    role TEXT NOT NULL CHECK(role IN ('bride','groom')), angle TEXT NOT NULL CHECK(angle IN ('front','left','right')),
    source_asset_id TEXT NOT NULL REFERENCES image_assets(id), makeup_level TEXT NOT NULL, hair_length TEXT NOT NULL,
    styling_version INTEGER NOT NULL, image_asset_id TEXT NOT NULL REFERENCES image_assets(id), prompt TEXT NOT NULL,
    created_at TEXT NOT NULL, discarded_at TEXT
);
CREATE INDEX IF NOT EXISTS styled_photo_key ON styled_photos(project_id,style_id,role,angle,source_asset_id);
-- V5.0: a look is one person's styled photo with a hairstyle and (bride) a makeup colour family, edited once from
-- the uploaded photo when the user asks for candidates; selected looks are what samples and photos are shot from.
-- A side look is derived from a side photo plus its front look (parent_look_id) when a shot needs it.
-- V7.0: a bride look is made for one style (style_id, with that style's makeup); groom looks have none and serve
-- every style.
-- V7.5: a bride front look is two edits, makeup (makeup_asset_id, with its own score) then hair (hair_prompt);
-- selected_by says whether the user picked the selected look or the most similar one was picked for them.
CREATE TABLE IF NOT EXISTS looks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    person TEXT NOT NULL CHECK(person IN ('a','b')), role TEXT NOT NULL CHECK(role IN ('bride','groom')),
    style_id TEXT,
    angle TEXT NOT NULL CHECK(angle IN ('front','left','right')), parent_look_id TEXT REFERENCES looks(id),
    hairstyle_id TEXT NOT NULL, hair_length TEXT NOT NULL, makeup_color TEXT, makeup_level TEXT NOT NULL,
    source_asset_id TEXT NOT NULL REFERENCES image_assets(id), styling_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting','running','ready','failed','interrupted')),
    error_code TEXT, error_message TEXT, model TEXT, prompt TEXT NOT NULL,
    image_asset_id TEXT REFERENCES image_assets(id), face_score REAL, selected_order INTEGER,
    client_request_id TEXT, created_at TEXT NOT NULL, finished_at TEXT, discarded_at TEXT,
    hair_prompt TEXT, makeup_asset_id TEXT REFERENCES image_assets(id), makeup_face_score REAL, selected_by TEXT,
    makeup_id TEXT REFERENCES makeups(id), makeup_style_id TEXT,
    jewelry_id TEXT,
    retouch_settings TEXT CHECK(retouch_settings IS NULL OR json_valid(retouch_settings))
);
CREATE INDEX IF NOT EXISTS look_project ON looks(project_id,person,angle);
CREATE INDEX IF NOT EXISTS look_queue ON looks(status,created_at);
CREATE INDEX IF NOT EXISTS look_request ON looks(client_request_id);
-- V7.8: the bride's makeup for one style is its own candidates: the makeup edit of the uploaded photo, hair
-- unchanged. One is selected per style (selected_by 'user' or 'auto', NULL when not selected), and every new bride
-- look of the style is a hair edit of that made-up photo (looks.makeup_id).
-- V7.21: a makeup or look can be reused by another style: the other style gets its own row on the same image
-- (no new edit). makeup_style_id is the style whose colour family and finish the makeup was painted in; NULL
-- means its own style_id. The bride's side looks and the shots describe her makeup by it, not by the shot's style.
-- V7.22: the groom's look is two edits too: his retouched photo (role 'groom', style_id and makeup_color NULL,
-- shared by every style like his looks), then the hair on it.
""" + MAKEUPS_TABLE.format(name='makeups') + MAKEUPS_INDEXES + """
-- V7.18: one person's final look: one edit of their selected look (look_id) into the whole person in one outfit.
-- Retired in V7.19: nothing writes or reads it any more. It stays so existing databases keep their rows, and the
-- pictures made with these images (recorded in their tasks' reference_image_ids) keep their files.
CREATE TABLE IF NOT EXISTS outfit_looks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    person TEXT NOT NULL CHECK(person IN ('a','b')), role TEXT NOT NULL CHECK(role IN ('bride','groom')),
    look_id TEXT NOT NULL REFERENCES looks(id), outfit_id TEXT NOT NULL,
    source_asset_id TEXT NOT NULL REFERENCES image_assets(id),
    status TEXT NOT NULL CHECK(status IN ('waiting','running','ready','failed','interrupted')),
    error_code TEXT, error_message TEXT, model TEXT, prompt TEXT NOT NULL,
    image_asset_id TEXT REFERENCES image_assets(id), selected_by TEXT,
    client_request_id TEXT, created_at TEXT NOT NULL, finished_at TEXT, discarded_at TEXT
);
CREATE INDEX IF NOT EXISTS outfit_look_key ON outfit_looks(look_id,outfit_id);
CREATE INDEX IF NOT EXISTS outfit_look_queue ON outfit_looks(status,created_at);
CREATE INDEX IF NOT EXISTS outfit_look_request ON outfit_looks(client_request_id);
CREATE TABLE IF NOT EXISTS project_outfits (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, style_id TEXT NOT NULL, outfit_id TEXT NOT NULL,
    PRIMARY KEY (project_id, style_id)
);
CREATE TABLE IF NOT EXISTS moment_drafts (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    preview_slot_id TEXT NOT NULL REFERENCES photo_slots(id), style_id TEXT NOT NULL REFERENCES style_presets(id),
    director_model TEXT, moments TEXT NOT NULL CHECK(json_valid(moments)), used_by_batch_id TEXT REFERENCES generation_batches(id),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- V7.33: one picture of a style's outfit standing in its main scene, shared by every project. Made in three edits
-- (product on white, empty scene, the two combined); files live under data/style_art/<id>/. A project's task
-- refers to it through an image_assets row of its own that points at the shared file. V7.34: room_id and area name
-- the room area of a style with rooms (app/rooms.py), one art each; both are null for a style's main-scene art.
CREATE TABLE IF NOT EXISTS style_arts (
    id TEXT PRIMARY KEY, style_id TEXT NOT NULL, outfit_id TEXT NOT NULL, art_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','ready','failed','interrupted')),
    model TEXT NOT NULL, product_path TEXT, scene_path TEXT, image_path TEXT,
    error_code TEXT, error_message TEXT, created_at TEXT NOT NULL, finished_at TEXT, discarded_at TEXT,
    room_id TEXT, area INTEGER, scene_version INTEGER, aspect_ratio TEXT
);
CREATE INDEX IF NOT EXISTS style_art_key ON style_arts(style_id,outfit_id,art_version,created_at);
-- V7.38: the picture of a room area other than the main one, made from the main picture when a shot first needs it
-- and shared by every project; files live under data/room_area/<id>/. V7.50: no longer made or read (a room has only
-- its main picture); kept for the rows already there.
CREATE TABLE IF NOT EXISTS room_areas (
    id TEXT PRIMARY KEY, style_id TEXT NOT NULL, room_id TEXT NOT NULL, area INTEGER NOT NULL, room_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','ready','failed','interrupted')),
    model TEXT NOT NULL, image_path TEXT, error_code TEXT, error_message TEXT,
    created_at TEXT NOT NULL, finished_at TEXT, discarded_at TEXT
);
"""

BATCH_REFERENCE_MODES = ('prompt_only', 'with_preview')
DIRECTOR_MODEL_KEYS = {'88api': 'director_model', 'pptoken': 'director_model_pptoken'}
DEFAULT_PPTOKEN_DIRECTOR_MODEL = 'gpt-6.1-sol'
# V7.1: replaces the on/off auto_face_swap setting, which is no longer read.
AUTO_FACE_FIX_MODES = ('split', 'ai', 'facefusion', 'off')
# V7.29: what each person gets after a picture is generated; the presets above are pairs of these.
FACE_FIX_METHODS = ('ai', 'facefusion', 'off')
AUTO_FACE_FIX_PRESETS = {'split': ('ai', 'facefusion'), 'ai': ('ai', 'ai'), 'facefusion': ('facefusion', 'facefusion'),
                         'off': ('off', 'off')}
IMAGE_PROVIDERS = tuple(PROVIDERS)
# Steps with their own image service and model in Settings ('{kind}_provider', '{kind}_model'); null follows the
# image service and the default model. 'face_fix' is the hand fix (its key predates V7.32); 2026-11-13, user: 每个步骤/
# 阶段的修图模型都可以自定义选择 — the AI face fix ('face') no longer shares the wash's, and the makeup, the look (side
# looks too), the style art and the regional edit's preselected model got their own.
STAGE_KINDS = ('makeup', 'look', 'style_art', 'face_fix', 'face', 'wash', 'regional')
DEFAULT_BATCH_REFERENCE_MODE = 'prompt_only'
# V7.33: whether new shots of a live style copy the clothes and the place from its style art.
STYLE_ART_MODES = ('on', 'off')
DEFAULT_STYLE_ART_MODE = 'on'


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self, *, write: bool = False):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=10000')
        try:
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, default_model: str = DEFAULT_MODEL, director_model: str | None = None,
                   portrait_ratio: str = DEFAULT_PORTRAIT_RATIO):
        from .prompts import STYLES

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            # Table rebuilds below must preserve version rows that point at tasks.
            connection.execute('PRAGMA foreign_keys=OFF')
            connection.executescript(SCHEMA)
            # Existing installations predate photo batches.  Upgrade in place and
            # keep every user row; SQLite has no safe ``CREATE OR REPLACE`` path.
            def add_column(table: str, definition: str):
                name = definition.split()[0]
                columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
                if name not in columns:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')

            add_column('style_arts', 'room_id TEXT')
            add_column('style_arts', 'area INTEGER')
            add_column('style_arts', 'scene_version INTEGER')
            add_column('style_arts', 'aspect_ratio TEXT')  # V7.49: the scene's shape
            add_column('generation_batches', 'style_id TEXT REFERENCES style_presets(id)')
            add_column('generation_batches', 'paused_at TEXT')
            add_column('looks', 'style_id TEXT')
            add_column('looks', 'hair_prompt TEXT')
            add_column('looks', 'makeup_asset_id TEXT REFERENCES image_assets(id)')
            add_column('looks', 'makeup_face_score REAL')
            add_column('looks', 'selected_by TEXT')
            add_column('looks', 'makeup_id TEXT REFERENCES makeups(id)')
            add_column('looks', 'makeup_style_id TEXT')
            add_column('looks', 'jewelry_id TEXT')  # V7.61: the bride's earrings and necklace, none if null
            add_column('makeups', 'makeup_style_id TEXT')
            self._upgrade_makeups(connection)
            for table in ('makeups', 'looks'):
                add_column(table, 'retouch_settings TEXT CHECK(retouch_settings IS NULL OR json_valid(retouch_settings))')
            add_column('image_assets', 'source_asset_id TEXT REFERENCES image_assets(id)')
            add_column('photo_versions', 'face_scores TEXT')
            add_column('photo_versions', 'size_warning TEXT')
            # Generation may finish before its background face score is ready.
            for table in ('photo_versions', 'looks', 'makeups'):
                add_column(table, 'face_score_pending INTEGER NOT NULL DEFAULT 0')
            add_column('projects', 'person_a_left_image_id TEXT REFERENCES image_assets(id)')
            add_column('projects', 'person_a_right_image_id TEXT REFERENCES image_assets(id)')
            add_column('projects', 'person_b_left_image_id TEXT REFERENCES image_assets(id)')
            add_column('projects', 'person_b_right_image_id TEXT REFERENCES image_assets(id)')
            # V7.48: a full side (profile) photo per direction next to the half side one.
            for person in ('a', 'b'):
                for side in ('left', 'right'):
                    add_column('projects', f'person_{person}_{side}_full_image_id TEXT REFERENCES image_assets(id)')
            add_column('projects', "makeup_level TEXT NOT NULL DEFAULT 'natural' CHECK(makeup_level IN ('light','natural','refined'))")
            add_column('projects', "groom_makeup_level TEXT NOT NULL DEFAULT 'natural' CHECK(groom_makeup_level IN ('light','natural','refined'))")
            for role in ('bride', 'groom'):
                add_column('projects', f'{role}_retouch_settings TEXT CHECK({role}_retouch_settings IS NULL OR json_valid({role}_retouch_settings))')
            add_column('projects', "bride_hair_length TEXT NOT NULL DEFAULT 'long' CHECK(bride_hair_length IN ('long','medium','short'))")
            add_column('projects', "groom_hair_length TEXT NOT NULL DEFAULT 'short' CHECK(groom_hair_length IN ('buzz','short','medium'))")
            add_column('generation_batches', 'reference_preview_version_id TEXT REFERENCES photo_versions(id)')
            add_column('photo_slots', 'pose_id TEXT')
            add_column('photo_slots', 'pose_name TEXT')
            add_column('photo_slots', 'shot_size TEXT')
            add_column('photo_slots', 'room_id TEXT')
            self._upgrade_preview_rooms(connection)
            add_column('generation_tasks', 'moment_title TEXT')
            add_column('generation_tasks', 'moment_shot_size TEXT')
            self._upgrade_generation_tasks(connection)
            # app_settings is the single source of truth for runtime settings.
            # The legacy settings table is kept only for one-time migration.
            settings_columns = {
                row[1] for row in connection.execute('PRAGMA table_info(settings)')
            } if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
            ).fetchone() else set()
            if settings_columns and 'batch_reference_mode' not in settings_columns:
                connection.execute(
                    "ALTER TABLE settings ADD COLUMN batch_reference_mode TEXT "
                    "CHECK(batch_reference_mode IN ('prompt_only','with_preview'))"
                )
                settings_columns.add('batch_reference_mode')
            connection.executemany(
                # Built-in styles follow the code, so prompt changes reach existing databases.
                'INSERT INTO style_presets VALUES (?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET '
                'name=excluded.name, description=excluded.description, prompt_template=excluded.prompt_template',
                [(s['id'], s['name'], s['description'], s['scene_prompt']) for s in STYLES],
            )
            connection.execute(
                "INSERT OR IGNORE INTO app_settings (key,value) VALUES ('default_model',?)",
                (default_model,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO app_settings (key,value) VALUES ('portrait_ratio',?)",
                (portrait_ratio if portrait_ratio in PORTRAIT_RATIOS else DEFAULT_PORTRAIT_RATIO,),
            )
            app_setting = connection.execute(
                "SELECT 1 FROM app_settings WHERE key='batch_reference_mode'"
            ).fetchone()
            if app_setting is None and settings_columns and 'batch_reference_mode' in settings_columns:
                legacy = connection.execute(
                    'SELECT batch_reference_mode FROM settings WHERE batch_reference_mode IS NOT NULL LIMIT 1'
                ).fetchone()
                if legacy and legacy[0] in BATCH_REFERENCE_MODES:
                    connection.execute(
                        "INSERT INTO app_settings (key,value) VALUES ('batch_reference_mode',?)",
                        (legacy[0],),
                    )
            connection.execute(
                "INSERT OR IGNORE INTO app_settings (key,value) VALUES ('batch_reference_mode',?)",
                (DEFAULT_BATCH_REFERENCE_MODE,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO app_settings (key,value) VALUES ('director_model',?)",
                (director_model or '',),
            )
            # 2026-11-13: the AI face fix used the wash's service and model (V7.32); it starts with what it used.
            if connection.execute("SELECT 1 FROM app_settings WHERE key='face_model'").fetchone() is None:
                for name in ('model', 'provider'):
                    connection.execute(
                        "INSERT OR IGNORE INTO app_settings (key,value) "
                        f"SELECT 'face_{name}', COALESCE((SELECT value FROM app_settings WHERE key='wash_{name}'), '')")
            connection.commit()
            connection.execute('PRAGMA foreign_keys=ON')
            violations = connection.execute('PRAGMA foreign_key_check').fetchall()
            if violations:
                raise sqlite3.IntegrityError('Foreign key violations found after schema upgrade')
        finally:
            connection.close()

    @staticmethod
    def _upgrade_preview_rooms(connection: sqlite3.Connection):
        """V7.56 (2026-11-05, user: 同一种风格不同场景都弄): a sample belongs to its style and its room, so one style
        has a sample per room. A sample from before keeps the room of its latest picture (from its task's snapshot)."""
        import json
        from .rooms import get_room
        rows = connection.execute(
            "SELECT s.id, s.style_id, s.current_version_id FROM photo_slots s WHERE s.kind='preview' AND s.room_id IS NULL").fetchall()
        for slot_id, style_id, version_id in rows:
            task = None
            if version_id is not None:
                task = connection.execute(
                    'SELECT t.model_config_snapshot FROM photo_versions v JOIN generation_tasks t ON t.id=v.generation_task_id '
                    'WHERE v.id=?', (version_id,)).fetchone()
            if task is None:
                task = connection.execute('SELECT model_config_snapshot FROM generation_tasks WHERE target_slot_id=? '
                                          'ORDER BY created_at DESC,rowid DESC LIMIT 1', (slot_id,)).fetchone()
            try:
                room = json.loads(task[0]).get('room') if task is not None else None
            except (TypeError, ValueError):
                room = None
            if isinstance(room, dict) and get_room(style_id, room.get('id')) is not None:
                connection.execute('UPDATE photo_slots SET room_id=? WHERE id=?', (room['id'], slot_id))
        connection.execute('DROP INDEX IF EXISTS preview_style')
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS preview_style_room ON photo_slots(project_id,style_id,COALESCE(room_id,'')) "
                           "WHERE kind='preview'")

    @staticmethod
    def _upgrade_makeups(connection: sqlite3.Connection):
        """V7.22: makeups get a role, and the groom's have no style or colour; every older one is the bride's."""
        columns = [row[1] for row in connection.execute('PRAGMA table_info(makeups)')]
        if 'role' in columns:
            return
        connection.execute(MAKEUPS_TABLE.format(name='makeups_v722'))
        names = ','.join(columns)
        connection.execute(f"INSERT INTO makeups_v722 ({names},role) SELECT {names},'bride' FROM makeups")
        connection.execute('DROP TABLE makeups')
        connection.execute('ALTER TABLE makeups_v722 RENAME TO makeups')
        connection.executescript(MAKEUPS_INDEXES)

    @staticmethod
    def _upgrade_generation_tasks(connection: sqlite3.Connection):
        columns = {row[1] for row in connection.execute('PRAGMA table_info(generation_tasks)')}
        foreign_keys = connection.execute('PRAGMA foreign_key_list(generation_tasks)').fetchall()
        slot_foreign_key = next((row for row in foreign_keys if row[3] == 'target_slot_id'), None)
        # A task keeps its former slot ID as an audit value after deletion, so
        # this column intentionally has no foreign key to photo_slots.
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='generation_tasks'"
        ).fetchone()
        table_sql = (sql_row[0] or '') if sql_row else ''
        needs_rebuild = (
            'regenerate_mode' not in columns or slot_foreign_key is not None
            or "'regional_edit'" not in table_sql or 'moment_title' not in columns
            or 'source_version_id' not in columns
        )
        if not needs_rebuild:
            return

        connection.execute('BEGIN IMMEDIATE')
        regenerate_expression = 'regenerate_mode' if 'regenerate_mode' in columns else 'NULL'
        source_version_expression = 'source_version_id' if 'source_version_id' in columns else 'NULL'
        connection.execute("""
            CREATE TABLE generation_tasks_new (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                batch_id TEXT NOT NULL REFERENCES generation_batches(id) ON DELETE CASCADE,
                target_slot_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('waiting','held','running','completed','failed','cancelled','interrupted')),
                reference_image_ids TEXT NOT NULL CHECK(json_valid(reference_image_ids)),
                style_id TEXT NOT NULL REFERENCES style_presets(id), pose_id TEXT,
                moment_title TEXT, moment_shot_size TEXT,
                prompt TEXT NOT NULL, model_config_snapshot TEXT NOT NULL CHECK(json_valid(model_config_snapshot)),
                provider_task_id TEXT, output_asset_id TEXT REFERENCES image_assets(id),
                error_code TEXT, error_message TEXT,
                auto_retry_count INTEGER NOT NULL DEFAULT 0 CHECK(auto_retry_count BETWEEN 0 AND 2),
                retry_of_task_id TEXT REFERENCES generation_tasks_new(id),
                regenerate_mode TEXT CHECK(regenerate_mode IS NULL OR regenerate_mode IN ('same','identity_boost','change_pose','change_shot','face_swap','face_fix','hand_fix','wash','regional_edit')),
                source_version_id TEXT,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
            )
        """)
        task_columns = (
            'id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,pose_id,prompt,'
            'model_config_snapshot,provider_task_id,output_asset_id,error_code,error_message,moment_title,moment_shot_size,'
            'auto_retry_count,retry_of_task_id,regenerate_mode,source_version_id,created_at,started_at,finished_at'
        )
        source_parts = []
        for name in task_columns.split(','):
            if name == 'regenerate_mode':
                source_parts.append(regenerate_expression)
            elif name == 'source_version_id':
                source_parts.append(source_version_expression)
            elif name in ('moment_title', 'moment_shot_size') and name not in columns:
                source_parts.append('NULL')
            else:
                source_parts.append(name)
        source_columns = ','.join(source_parts)
        connection.execute(
            f'INSERT INTO generation_tasks_new ({task_columns}) SELECT {source_columns} FROM generation_tasks'
        )
        connection.execute('DROP TABLE generation_tasks')
        connection.execute('ALTER TABLE generation_tasks_new RENAME TO generation_tasks')
        connection.execute('CREATE INDEX task_queue ON generation_tasks(status,created_at)')
        connection.execute('CREATE INDEX task_batch ON generation_tasks(batch_id)')
        connection.execute('CREATE INDEX task_project ON generation_tasks(project_id)')
        connection.execute('CREATE INDEX task_slot ON generation_tasks(target_slot_id,created_at)')
        connection.execute(
            'CREATE UNIQUE INDEX task_retry_once ON generation_tasks(retry_of_task_id) '
            'WHERE retry_of_task_id IS NOT NULL'
        )
        connection.commit()

    def get_default_model(self) -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key='default_model'").fetchone()
            return row['value'] if row else DEFAULT_MODEL

    def set_default_model(self, model: str):
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO app_settings (key,value) VALUES ('default_model',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (model,),
            )

    def get_batch_reference_mode(self) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key='batch_reference_mode'"
            ).fetchone()
        value = row['value'] if row else DEFAULT_BATCH_REFERENCE_MODE
        return value if value in BATCH_REFERENCE_MODES else DEFAULT_BATCH_REFERENCE_MODE

    def set_batch_reference_mode(self, mode: str):
        if mode not in BATCH_REFERENCE_MODES:
            raise ValueError('invalid batch reference mode')
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO app_settings (key,value) VALUES ('batch_reference_mode',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (mode,),
            )

    def get_style_art_mode(self) -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key='style_art_mode'").fetchone()
        value = row['value'] if row else DEFAULT_STYLE_ART_MODE
        return value if value in STYLE_ART_MODES else DEFAULT_STYLE_ART_MODE

    def set_style_art_mode(self, mode: str):
        if mode not in STYLE_ART_MODES:
            raise ValueError('invalid style art mode')
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO app_settings (key,value) VALUES ('style_art_mode',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (mode,),
            )

    def get_portrait_ratio(self) -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key='portrait_ratio'").fetchone()
        value = row['value'] if row else DEFAULT_PORTRAIT_RATIO
        return value if value in PORTRAIT_RATIOS else DEFAULT_PORTRAIT_RATIO

    def set_portrait_ratio(self, ratio: str):
        if ratio not in PORTRAIT_RATIOS:
            raise ValueError('invalid portrait ratio')
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO app_settings (key,value) VALUES ('portrait_ratio',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (ratio,),
            )

    def get_auto_face_fix_roles(self) -> dict[str, str]:
        """What is done to each person in every generated picture with faces (V7.29): 'ai' (the AI face fix),
        'facefusion' (FaceFusion's swap) or 'off'. Before V7.29 one preset set both (V7.1, V7.13); a database that
        only has the preset reads as its pair, 'split' (bride AI, groom FaceFusion) when there is nothing."""
        with self.connect() as connection:
            rows = dict(connection.execute(
                "SELECT key,value FROM app_settings WHERE key IN ('auto_face_fix','auto_face_fix_bride','auto_face_fix_groom')"
            ).fetchall())
        preset = AUTO_FACE_FIX_PRESETS.get(rows.get('auto_face_fix'), AUTO_FACE_FIX_PRESETS['split'])
        return {role: rows[f'auto_face_fix_{role}'] if rows.get(f'auto_face_fix_{role}') in FACE_FIX_METHODS else default
                for role, default in zip(('bride', 'groom'), preset)}

    def set_auto_face_fix_roles(self, *, bride: str | None = None, groom: str | None = None):
        current = self.get_auto_face_fix_roles()
        chosen = {'bride': bride or current['bride'], 'groom': groom or current['groom']}
        if any(method not in FACE_FIX_METHODS for method in chosen.values()):
            raise ValueError(f'unknown face fix method {chosen}')
        with self.connect(write=True) as connection:
            for role, method in chosen.items():
                connection.execute('INSERT INTO app_settings (key,value) VALUES (?,?) '
                                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (f'auto_face_fix_{role}', method))

    def get_auto_face_fix(self) -> str:
        """The preset the two people's methods make (V7.1, V7.13), or 'custom' for another pair (V7.29)."""
        pair = tuple(self.get_auto_face_fix_roles().values())
        return next((name for name, preset in AUTO_FACE_FIX_PRESETS.items() if preset == pair), 'custom')

    def set_auto_face_fix(self, mode: str):
        """Set both people from a preset."""
        if mode not in AUTO_FACE_FIX_MODES:
            raise ValueError(f'unknown auto face fix mode {mode}')
        bride, groom = AUTO_FACE_FIX_PRESETS[mode]
        self.set_auto_face_fix_roles(bride=bride, groom=groom)

    def get_stage_model(self, kind: str) -> str | None:
        """The image model one step uses (STAGE_KINDS); None follows the default model (also when the stored model has
        left the registry)."""
        if kind not in STAGE_KINDS:
            raise ValueError('unsupported stage')
        with self.connect() as connection:
            row = connection.execute('SELECT value FROM app_settings WHERE key=?', (f'{kind}_model',)).fetchone()
        return row['value'] if row is not None and row['value'] in MODELS_BY_ID else None

    def set_stage_model(self, kind: str, model: str | None):
        if kind not in STAGE_KINDS:
            raise ValueError('unsupported stage')
        with self.connect(write=True) as connection:
            connection.execute('INSERT INTO app_settings (key,value) VALUES (?,?) '
                               'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (f'{kind}_model', model or ''))

    def get_edit_provider(self, kind: str) -> str | None:
        """The image service of one step (STAGE_KINDS; V7.27); None follows the image service of everything else."""
        with self.connect() as connection:
            row = connection.execute('SELECT value FROM app_settings WHERE key=?', (f'{kind}_provider',)).fetchone()
        return row['value'] if row is not None and row['value'] in IMAGE_PROVIDERS else None

    def set_edit_provider(self, kind: str, provider: str | None):
        if provider is not None and provider not in IMAGE_PROVIDERS:
            raise ValueError('unsupported image provider')
        with self.connect(write=True) as connection:
            connection.execute('INSERT INTO app_settings (key,value) VALUES (?,?) '
                               'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (f'{kind}_provider', provider or ''))

    def get_edit_choice(self, kind: str) -> tuple[str, str]:
        """(service, model) a new request of one step (STAGE_KINDS) uses (V7.27): the step's own service, else the
        image service; its own model when that service offers it, else the default model when it does, else the
        service's first model."""
        provider = self.get_edit_provider(kind) or self.get_image_provider()
        model = self.get_stage_model(kind)
        if model is None or provider not in MODELS_BY_ID[model].providers:
            default = self.get_default_model()
            model = default if default in MODELS_BY_ID and provider in MODELS_BY_ID[default].providers \
                else provider_models(provider)[0].id
        return provider, model

    def get_image_provider(self) -> str:
        """The image service models both services offer go to (V7.11); 88API until Settings changes it."""
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key='image_provider'").fetchone()
        return row['value'] if row is not None and row['value'] in IMAGE_PROVIDERS else '88api'

    def set_image_provider(self, provider: str):
        if provider not in IMAGE_PROVIDERS:
            raise ValueError('unsupported image provider')
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO app_settings (key,value) VALUES ('image_provider',?) "
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (provider,))

    def get_director_provider(self) -> str:
        """The service the AI director's text requests go to; separate from the image service."""
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key='director_provider'").fetchone()
        return row['value'] if row is not None and row['value'] in DIRECTOR_PROVIDERS else '88api'

    def set_director_provider(self, provider: str):
        if provider not in DIRECTOR_PROVIDERS:
            raise ValueError('unsupported director provider')
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO app_settings (key,value) VALUES ('director_provider',?) "
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (provider,))

    def get_director_model(self) -> str | None:
        """The director's model on its current service. Each service remembers its own, since their model names differ."""
        provider = self.get_director_provider()
        with self.connect() as connection:
            row = connection.execute('SELECT value FROM app_settings WHERE key=?', (DIRECTOR_MODEL_KEYS[provider],)).fetchone()
        if row is None:
            return DEFAULT_PPTOKEN_DIRECTOR_MODEL if provider == 'pptoken' else None
        return row['value'] or None

    def set_director_model(self, model: str | None):
        """Set the model of the director's current service."""
        key = DIRECTOR_MODEL_KEYS[self.get_director_provider()]
        with self.connect(write=True) as connection:
            connection.execute(
                'INSERT INTO app_settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, model or ''),
            )
