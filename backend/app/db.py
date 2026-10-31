"""SQLite persistence; every operation owns its connection and transaction."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .aspect import DEFAULT_PORTRAIT_RATIO, PORTRAIT_RATIOS
from .models import DEFAULT_MODEL


def new_id() -> str:
    return uuid4().hex


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    person_a_image_id TEXT REFERENCES image_assets(id),
    person_a_left_image_id TEXT REFERENCES image_assets(id),
    person_a_right_image_id TEXT REFERENCES image_assets(id),
    person_a_role TEXT NOT NULL DEFAULT 'bride' CHECK(person_a_role IN ('bride','groom')),
    person_b_image_id TEXT REFERENCES image_assets(id),
    person_b_left_image_id TEXT REFERENCES image_assets(id),
    person_b_right_image_id TEXT REFERENCES image_assets(id),
    selected_style_id TEXT REFERENCES style_presets(id),
    selected_preview_version_id TEXT REFERENCES photo_versions(id),
    makeup_level TEXT NOT NULL DEFAULT 'natural' CHECK(makeup_level IN ('light','natural','refined')),
    groom_makeup_level TEXT NOT NULL DEFAULT 'natural' CHECK(groom_makeup_level IN ('light','natural','refined')),
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
    reference_preview_version_id TEXT REFERENCES photo_versions(id)
);
CREATE TABLE IF NOT EXISTS photo_slots (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES generation_batches(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('preview','photo')),
    style_id TEXT NOT NULL REFERENCES style_presets(id),
    current_version_id TEXT REFERENCES photo_versions(id), created_at TEXT NOT NULL,
    pose_id TEXT, pose_name TEXT, shot_size TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS preview_style ON photo_slots(project_id,style_id) WHERE kind='preview';
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
    regenerate_mode TEXT CHECK(regenerate_mode IS NULL OR regenerate_mode IN ('same','identity_boost','change_pose','change_shot','face_swap')),
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
CREATE TABLE IF NOT EXISTS moment_drafts (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    preview_slot_id TEXT NOT NULL REFERENCES photo_slots(id), style_id TEXT NOT NULL REFERENCES style_presets(id),
    director_model TEXT, moments TEXT NOT NULL CHECK(json_valid(moments)), used_by_batch_id TEXT REFERENCES generation_batches(id),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

BATCH_REFERENCE_MODES = ('prompt_only', 'with_preview')
DEFAULT_BATCH_REFERENCE_MODE = 'prompt_only'


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

            add_column('generation_batches', 'style_id TEXT REFERENCES style_presets(id)')
            add_column('image_assets', 'source_asset_id TEXT REFERENCES image_assets(id)')
            add_column('photo_versions', 'face_scores TEXT')
            add_column('projects', 'person_a_left_image_id TEXT REFERENCES image_assets(id)')
            add_column('projects', 'person_a_right_image_id TEXT REFERENCES image_assets(id)')
            add_column('projects', 'person_b_left_image_id TEXT REFERENCES image_assets(id)')
            add_column('projects', 'person_b_right_image_id TEXT REFERENCES image_assets(id)')
            add_column('projects', "makeup_level TEXT NOT NULL DEFAULT 'natural' CHECK(makeup_level IN ('light','natural','refined'))")
            add_column('projects', "groom_makeup_level TEXT NOT NULL DEFAULT 'natural' CHECK(groom_makeup_level IN ('light','natural','refined'))")
            add_column('projects', "bride_hair_length TEXT NOT NULL DEFAULT 'long' CHECK(bride_hair_length IN ('long','medium','short'))")
            add_column('projects', "groom_hair_length TEXT NOT NULL DEFAULT 'short' CHECK(groom_hair_length IN ('buzz','short','medium'))")
            add_column('generation_batches', 'reference_preview_version_id TEXT REFERENCES photo_versions(id)')
            add_column('photo_slots', 'pose_id TEXT')
            add_column('photo_slots', 'pose_name TEXT')
            add_column('photo_slots', 'shot_size TEXT')
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
            connection.commit()
            connection.execute('PRAGMA foreign_keys=ON')
            violations = connection.execute('PRAGMA foreign_key_check').fetchall()
            if violations:
                raise sqlite3.IntegrityError('Foreign key violations found after schema upgrade')
        finally:
            connection.close()

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
            or 'face_swap' not in table_sql or 'moment_title' not in columns
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
                regenerate_mode TEXT CHECK(regenerate_mode IS NULL OR regenerate_mode IN ('same','identity_boost','change_pose','change_shot','face_swap')),
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

    def get_auto_face_swap(self) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key='auto_face_swap'").fetchone()
        # On by default: every generated image is face-swapped once; the original stays in the version history.
        return row is None or row['value'] == '1'

    def set_auto_face_swap(self, enabled: bool):
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO app_settings (key,value) VALUES ('auto_face_swap',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ('1' if enabled else '0',),
            )

    def get_director_model(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key='director_model'").fetchone()
        return row['value'] or None if row else None

    def set_director_model(self, model: str | None):
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO app_settings (key,value) VALUES ('director_model',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (model or '',),
            )
