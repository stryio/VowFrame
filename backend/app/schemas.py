"""HTTP request and response models matching docs/API.md."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal['bride', 'groom']
TaskStatus = Literal['waiting', 'held', 'running', 'completed', 'failed', 'cancelled', 'interrupted']
# Shots written from now on; stored works may also carry the retired V3.4–V3.11 ladder.
ShotSize = Literal['wide', 'full', 'three_quarter', 'half', 'close']
StoredShotSize = Literal['full', 'three_quarter', 'half', 'close', 'wide', 'medium', 'extreme_close', 'detail']


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ProjectCreate(InputModel):
    name: str = '我的婚纱照'


MakeupLevel = Literal['light', 'natural', 'refined']


class ProjectPatch(InputModel):
    name: str | None = None
    person_a_role: Role | None = None
    makeup_level: MakeupLevel | None = None
    groom_makeup_level: MakeupLevel | None = None

    @field_validator('name', 'person_a_role', 'makeup_level', 'groom_makeup_level')
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class PreviewCreate(InputModel):
    client_request_id: str = Field(min_length=1)
    style_ids: list[str] | None = None
    model: str | None = None

    @field_validator('model')
    @classmethod
    def reject_null_model(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class PhotoBatchCreate(InputModel):
    client_request_id: str = Field(min_length=1)
    draft_id: str
    model: str | None = None

    @field_validator('model')
    @classmethod
    def reject_null_model(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class MomentDraftCreate(InputModel):
    preview_slot_id: str
    count: int


class RegenerateCreate(InputModel):
    client_request_id: str = Field(min_length=1)
    mode: Literal['same', 'identity_boost']
    model: str | None = None

    @field_validator('model')
    @classmethod
    def reject_null_model(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class ChangePoseCreate(InputModel):
    client_request_id: str = Field(min_length=1)
    model: str | None = None

    @field_validator('model')
    @classmethod
    def reject_null_model(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class BatchActionCreate(InputModel):
    client_request_id: str = Field(min_length=1)
    slot_ids: list[str]
    action: Literal['same', 'identity_boost', 'change_pose', 'change_shot', 'face_swap']
    shot_size: ShotSize | None = None
    model: str | None = None

    @field_validator('model')
    @classmethod
    def reject_null_model(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class RestoreCreate(InputModel):
    version_id: str


class ExportCreate(InputModel):
    slot_ids: list[str] | None = None


class SettingsPatch(InputModel):
    default_model: str | None = None
    batch_reference_mode: str | None = None
    director_model: str | None = None
    portrait_ratio: str | None = None
    auto_face_swap: bool | None = None

    @field_validator('default_model', 'batch_reference_mode', 'portrait_ratio', 'auto_face_swap')
    @classmethod
    def reject_null_default_model(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class Settings(BaseModel):
    default_model: str
    batch_reference_mode: Literal['prompt_only', 'with_preview']
    director_model: str | None
    portrait_ratio: Literal['3:4', '2:3', '9:16']
    auto_face_swap: bool


class Model(BaseModel):
    id: str
    name: str
    family: Literal['openai', 'gemini']
    max_reference_images: int
    description: str


class Style(BaseModel):
    id: str
    name: str
    description: str
    default_preview: bool
    retired: bool


class Person(BaseModel):
    asset_id: str | None
    left_asset_id: str | None = None
    right_asset_id: str | None = None
    role: Role


class FaceScores(BaseModel):
    """Similarity to each person's front photo (FaceFusion's ArcFace); null when that face was not found."""
    bride: float | None
    groom: float | None


class VersionRef(BaseModel):
    id: str
    asset_id: str
    version_number: int
    created_at: str
    model: str | None
    source_version_id: str | None
    face_scores: FaceScores | None = None


class VersionDetail(BaseModel):
    id: str
    asset_id: str
    version_number: int
    created_at: str
    model: str | None
    pose_id: str | None
    pose_name: str | None
    shot_size: StoredShotSize | None = None
    regenerate_mode: Literal['same', 'identity_boost', 'change_pose', 'change_shot', 'face_swap'] | None
    source_version_id: str | None
    is_current: bool
    face_scores: FaceScores | None = None


class TaskBrief(BaseModel):
    id: str
    status: TaskStatus
    error_code: str | None
    error_message: str | None
    model: str | None
    regenerate_mode: Literal['same', 'identity_boost', 'change_pose', 'change_shot', 'face_swap'] | None


class Slot(BaseModel):
    id: str
    kind: Literal['preview', 'photo']
    style_id: str
    style_name: str
    batch_id: str
    pose_id: str | None
    pose_name: str | None
    shot_size: StoredShotSize | None = None
    current_version: VersionRef | None
    version_count: int
    latest_task: TaskBrief | None
    is_stale: bool


class PhotoSet(BaseModel):
    style_id: str
    style_name: str
    slot_count: int
    completed_count: int
    batch_ids: list[str]


class ProjectSummary(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    stage: Literal['upload', 'style', 'shooting']
    cover_asset_id: str | None
    photo_count: int
    active_task_count: int
    has_pending_recovery: bool


class ProjectDetail(ProjectSummary):
    makeup_level: MakeupLevel
    groom_makeup_level: MakeupLevel
    person_a: Person
    person_b: Person
    previews: list[Slot]
    photo_sets: list[PhotoSet]
    photos: list[Slot]


class Batch(BaseModel):
    id: str
    project_id: str
    batch_type: Literal['preview', 'photo', 'regenerate']
    style_id: str | None
    style_name: str | None
    created_at: str
    status: Literal['running', 'held', 'needs_attention', 'completed']
    total: int
    counts: dict[TaskStatus, int]


class Task(BaseModel):
    id: str
    project_id: str
    batch_id: str
    target_slot_id: str
    status: TaskStatus
    style_id: str
    pose_id: str | None
    moment_title: str | None = None
    moment_shot_size: StoredShotSize | None = None
    prompt: str
    error_code: str | None
    error_message: str | None
    auto_retry_count: int
    retry_of_task_id: str | None
    regenerate_mode: Literal['same', 'identity_boost', 'change_pose', 'change_shot', 'face_swap'] | None
    source_version_id: str | None
    model: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class Moment(BaseModel):
    title: str
    shot_size: ShotSize
    orientation: Literal['portrait', 'landscape']
    bride_facing: Literal['front', 'left', 'right']
    groom_facing: Literal['front', 'left', 'right']
    text: str
    scene: int | None = None
    faces_clear: bool = True


class MomentDraft(BaseModel):
    id: str
    project_id: str
    preview_slot_id: str
    style_id: str
    style_name: str
    director_model: str | None
    moments: list[Moment]
    used_by_batch_id: str | None
    created_at: str
    updated_at: str


class MomentPatch(InputModel):
    title: str | None = None
    shot_size: ShotSize | None = None
    orientation: Literal['portrait', 'landscape'] | None = None
    bride_facing: Literal['front', 'left', 'right'] | None = None
    groom_facing: Literal['front', 'left', 'right'] | None = None
    text: str | None = None
    faces_clear: bool | None = None


class MomentRewrite(InputModel):
    shot_size: ShotSize | None = None


class ChangeShotCreate(InputModel):
    client_request_id: str = Field(min_length=1)
    shot_size: ShotSize
    model: str | None = None

    @field_validator('model')
    @classmethod
    def reject_null_model(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class FaceSwapCreate(InputModel):
    client_request_id: str = Field(min_length=1)
