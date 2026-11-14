"""HTTP request and response models matching docs/API.md."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .retouch import RetouchSettings

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
BrideHairLength = Literal['long', 'medium', 'short']
GroomHairLength = Literal['buzz', 'short', 'medium']


class ProjectPatch(InputModel):
    name: str | None = None
    person_a_role: Role | None = None
    makeup_level: MakeupLevel | None = None
    groom_makeup_level: MakeupLevel | None = None
    bride_retouch_settings: RetouchSettings | None = None
    groom_retouch_settings: RetouchSettings | None = None
    bride_hair_length: BrideHairLength | None = None
    groom_hair_length: GroomHairLength | None = None

    @field_validator('name', 'person_a_role', 'makeup_level', 'groom_makeup_level', 'bride_hair_length', 'groom_hair_length',
                     'bride_retouch_settings', 'groom_retouch_settings')
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class PreviewCreate(InputModel):
    client_request_id: str = Field(min_length=1)
    style_ids: list[str] | None = None
    model: str | None = None
    # V7.34: the room each style with rooms is shot in ({style_id: room_id}); a style left out takes its first room.
    # V7.56: or several rooms ({style_id: [room_id, ...]}), one sample each; a style has a sample per room.
    rooms: dict[str, str | list[str]] | None = None
    # V7.37: the outfit each style is shot in ({style_id: outfit_id}), picked with the room; it also becomes the
    # project's choice for the style. A style left out wears the project's choice.
    outfits: dict[str, str] | None = None
    # Each sample is shot from its style's selected bride look and the selected groom look (V7.0).

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
    action: Literal['same', 'identity_boost', 'change_pose', 'change_shot', 'face_swap', 'face_fix']
    shot_size: ShotSize | None = None
    model: str | None = None

    @field_validator('model')
    @classmethod
    def reject_null_model(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class LookItem(InputModel):
    # The hairstyle; the bride's makeup colour and finish come from the style (V7.0).
    hairstyle_id: str
    # V7.61: the bride's earrings and necklace (prompts.JEWELRY); none when omitted. Not for the groom.
    jewelry_id: str | None = None


class LookCreate(InputModel):
    client_request_id: str = Field(min_length=1)
    person: Literal['a', 'b']
    # The style a bride look is made for (V7.0); omitted for the groom, whose looks every style shares.
    style_id: str | None = None
    items: list[LookItem] = Field(min_length=1, max_length=16)


class MakeupCreate(InputModel):
    """Makeup candidates of the bride for one style (V7.8), or the groom's retouched photos (V7.22, no style): each
    is one paid edit of the person's uploaded photo."""
    client_request_id: str = Field(min_length=1)
    person: Literal['a', 'b']
    style_id: str | None = None
    count: int = Field(ge=1, le=4)


class MakeupReuse(InputModel):
    """Use a ready makeup of another style for this style too, with the looks made on it (V7.21); no new edit."""
    style_id: str


class RestoreCreate(InputModel):
    version_id: str


class ExportCreate(InputModel):
    slot_ids: list[str] | None = None


class SettingsPatch(InputModel):
    default_model: str | None = None
    batch_reference_mode: str | None = None
    director_model: str | None = None
    portrait_ratio: str | None = None
    # V7.1: replaces auto_face_swap. A preset that sets both people (V7.29: auto_face_fix_bride / _groom set one).
    auto_face_fix: Literal['split', 'ai', 'facefusion', 'off'] | None = None
    auto_face_fix_bride: Literal['ai', 'facefusion', 'off'] | None = None
    auto_face_fix_groom: Literal['ai', 'facefusion', 'off'] | None = None
    # The AI face fix's image model; null follows the default model.
    face_fix_model: str | None = None
    # V7.26: the wash's image model; null follows the default model.
    wash_model: str | None = None
    # V7.27: the image service of face fixes (hand fixes too) and of washes; null follows image_provider.
    face_fix_provider: Literal['88api', 'pptoken', 'ark'] | None = None
    wash_provider: Literal['88api', 'pptoken', 'ark'] | None = None
    # 2026-11-13: the other steps' own service and model (db.STAGE_KINDS); null follows image_provider / default_model.
    face_model: str | None = None
    face_provider: Literal['88api', 'pptoken', 'ark'] | None = None
    regional_model: str | None = None
    regional_provider: Literal['88api', 'pptoken', 'ark'] | None = None
    makeup_model: str | None = None
    makeup_provider: Literal['88api', 'pptoken', 'ark'] | None = None
    look_model: str | None = None
    look_provider: Literal['88api', 'pptoken', 'ark'] | None = None
    style_art_model: str | None = None
    style_art_provider: Literal['88api', 'pptoken', 'ark'] | None = None
    # V7.11: the image service, '88api', 'pptoken' or 'ark' (2026-11-13).
    image_provider: Literal['88api', 'pptoken', 'ark'] | None = None
    # The service the AI director uses; director_model is that service's model.
    director_provider: Literal['88api', 'pptoken'] | None = None
    # V7.33: whether new shots of a live style copy the clothes and the place from its style art.
    style_art_mode: Literal['on', 'off'] | None = None

    @field_validator('default_model', 'batch_reference_mode', 'portrait_ratio', 'auto_face_fix')
    @classmethod
    def reject_null_default_model(cls, value):
        if value is None:
            raise ValueError('字段不能为 null')
        return value


class ImageProvider(BaseModel):
    id: Literal['88api', 'pptoken', 'ark']
    name: str
    available: bool
    # Why it cannot be selected, written for the user; null when it can.
    reason: str | None


class Settings(BaseModel):
    default_model: str
    batch_reference_mode: Literal['prompt_only', 'with_preview']
    director_model: str | None
    portrait_ratio: Literal['3:4', '2:3', '9:16']
    # The preset the two people's methods make, or 'custom' for another pair (V7.29).
    auto_face_fix: Literal['split', 'ai', 'facefusion', 'off', 'custom']
    # V7.29: what each person gets after a picture is generated.
    auto_face_fix_bride: Literal['ai', 'facefusion', 'off']
    auto_face_fix_groom: Literal['ai', 'facefusion', 'off']
    # Null: AI face fixes use default_model.
    face_fix_model: str | None
    # V7.26: null, washes use default_model.
    wash_model: str | None
    # V7.27: the image service of face fixes (hand fixes too) and of washes; null follows image_provider.
    face_fix_provider: Literal['88api', 'pptoken', 'ark'] | None
    wash_provider: Literal['88api', 'pptoken', 'ark'] | None
    # 2026-11-13: the other steps' own service and model; null follows image_provider / default_model.
    face_model: str | None
    face_provider: Literal['88api', 'pptoken', 'ark'] | None
    regional_model: str | None
    regional_provider: Literal['88api', 'pptoken', 'ark'] | None
    makeup_model: str | None
    makeup_provider: Literal['88api', 'pptoken', 'ark'] | None
    look_model: str | None
    look_provider: Literal['88api', 'pptoken', 'ark'] | None
    style_art_model: str | None
    style_art_provider: Literal['88api', 'pptoken', 'ark'] | None
    # V7.11: the image service models both services offer go to, and whether each one can be selected.
    image_provider: Literal['88api', 'pptoken', 'ark']
    image_providers: list[ImageProvider]
    director_provider: Literal['88api', 'pptoken']
    director_providers: list[ImageProvider]
    style_art_mode: Literal['on', 'off']


class StyleArt(BaseModel):
    """A live style's art (V7.33): its outfit, worn by no one, standing in its main scene."""
    style_id: str
    style_name: str
    outfit_id: str
    outfit_name: str
    # V7.34: the room this art shows, for a style with rooms (V7.50: one art per room, of its main picture, area 1);
    # null for a main-scene art.
    room_id: str | None = None
    room_name: str | None = None
    area: int | None = None
    area_name: str | None = None
    area_image_url: str | None = None
    # The latest attempt: null when the style has none yet (it is made when a shot first needs it).
    id: str | None = None
    status: Literal['running', 'ready', 'failed', 'interrupted'] | None = None
    model: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    # URLs of the three pictures (outfit on white, empty scene, the two together); null until made.
    product_url: str | None = None
    scene_url: str | None = None
    image_url: str | None = None
    # What new shots use when the latest attempt failed: the earlier ready art, if any.
    in_use_id: str | None = None
    # Paid edits that making it (again) costs now (V7.35): 3 for a main-scene art; 1 for a room, 2 while its outfit
    # has no outfit-on-white picture yet.
    cost: int = 3


class Model(BaseModel):
    id: str
    name: str
    family: Literal['openai', 'gemini', 'seedream']
    max_reference_images: int
    description: str
    # The image services offering it (V7.11).
    providers: list[Literal['88api', 'pptoken', 'ark']]


class RoomAreaBrief(BaseModel):
    name: str
    # The room's main picture for area 1; null for the other areas, which have no picture of their own (V7.50).
    image_url: str | None = None


class RoomBrief(BaseModel):
    """A room a style is shot in (V7.34): its areas, the first being the room's main picture."""
    id: str
    name: str
    areas: list[RoomAreaBrief]


class Style(BaseModel):
    id: str
    name: str
    description: str
    default_preview: bool
    retired: bool
    # V7.34: the rooms a sample of this style can be shot in; empty for a style without rooms.
    rooms: list[RoomBrief] = []


class ShotRoom(BaseModel):
    """The room (V7.34) a picture was shot in, and the area (1 is the room's main picture)."""
    id: str
    name: str
    area: int | None = None
    area_name: str | None = None


class Person(BaseModel):
    asset_id: str | None
    left_asset_id: str | None = None
    right_asset_id: str | None = None
    # V7.48: the full side (profile) photos; left/right above are the half side ones.
    left_full_asset_id: str | None = None
    right_full_asset_id: str | None = None
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
    # V7.25: how this version was made ('face_swap', 'face_fix', 'hand_fix', …); null for a first generation.
    regenerate_mode: Literal['same', 'identity_boost', 'change_pose', 'change_shot', 'face_swap', 'face_fix', 'hand_fix', 'wash', 'regional_edit'] | None = None
    face_scores: FaceScores | None = None
    # The service returned another size than requested, e.g. '要求 1536×2048，实际 1086×1448'; null when it did not.
    size_warning: str | None = None


class VersionDetail(BaseModel):
    id: str
    asset_id: str
    version_number: int
    created_at: str
    model: str | None
    pose_id: str | None
    pose_name: str | None
    shot_size: StoredShotSize | None = None
    regenerate_mode: Literal['same', 'identity_boost', 'change_pose', 'change_shot', 'face_swap', 'face_fix', 'hand_fix', 'wash', 'regional_edit'] | None
    source_version_id: str | None
    is_current: bool
    face_scores: FaceScores | None = None
    # The service returned another size than requested, e.g. '要求 1536×2048，实际 1086×1448'; null when it did not.
    size_warning: str | None = None


class TaskBrief(BaseModel):
    id: str
    status: TaskStatus
    error_code: str | None
    error_message: str | None
    model: str | None
    regenerate_mode: Literal['same', 'identity_boost', 'change_pose', 'change_shot', 'face_swap', 'face_fix', 'hand_fix', 'wash', 'regional_edit'] | None


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
    # The styled photos the current version was shot from (V4.0); null for older pictures.
    styled_asset_ids: dict[str, str] | None = None
    # The looks the current version was shot from (V5.0); null for older pictures.
    look_ids: dict[str, str] | None = None
    # The room the current version was shot in (V7.34); null for styles without rooms and older pictures.
    room: ShotRoom | None = None
    # The style art the current version was shot from (V7.35): its image asset; null without one.
    style_art_asset_id: str | None = None


class Look(BaseModel):
    id: str
    person: Literal['a', 'b']
    role: Role
    # The style a bride look was made for (V7.0); null for the groom's and for looks of older versions.
    style_id: str | None
    hairstyle_id: str
    hairstyle_name: str
    # V7.61: the earrings and necklace the bride wears on this look; null for none.
    jewelry_id: str | None = None
    jewelry_name: str | None = None
    makeup_color: str | None
    makeup_color_name: str | None
    makeup_level: MakeupLevel
    hair_length: str
    retouch_settings: RetouchSettings
    status: Literal['waiting', 'running', 'ready', 'failed', 'interrupted']
    error_code: str | None
    error_message: str | None
    asset_id: str | None
    source_asset_id: str
    selected: bool
    selected_order: int | None
    usable: bool
    unusable_reason: str | None
    # Likeness to the uploaded photo (FaceFusion's ArcFace cosine similarity); null until measured.
    face_score: float | None
    created_at: str
    # V7.5: the bride's made-up photo before the hair edit, and its likeness; null for one-edit looks.
    makeup_asset_id: str | None
    makeup_face_score: float | None
    # 'user' when the user picked the selected look, 'auto' when it was picked as the most similar; null otherwise.
    selected_by: Literal['user', 'auto'] | None
    # V7.8: the makeup candidate the hair was done on; null for the groom's and for looks made before V7.8.
    makeup_id: str | None
    # V7.21: the style whose colour family and finish the bride's makeup was painted in (style_id unless the look
    # was reused from another style); null for the groom's.
    makeup_style_id: str | None


class Makeup(BaseModel):
    """The bride's makeup for one style, hair unchanged (V7.8); new bride looks of the style use the selected one.
    V7.22: the groom's retouched photo, hair unchanged (role 'groom', no style or colour); his new looks use it."""
    id: str
    person: Literal['a', 'b']
    role: Role
    style_id: str | None
    # V7.21: the style whose colour family and finish it was painted in; style_id unless reused from another style.
    makeup_style_id: str | None
    style_name: str | None
    makeup_style_name: str | None
    makeup_color: str | None
    makeup_color_name: str | None
    makeup_level: MakeupLevel
    status: Literal['waiting', 'running', 'ready', 'failed', 'interrupted']
    retouch_settings: RetouchSettings
    error_code: str | None
    error_message: str | None
    asset_id: str | None
    source_asset_id: str
    selected: bool
    # 'user' when the user picked it, 'auto' when it was picked as the most similar; null when not selected.
    selected_by: Literal['user', 'auto'] | None
    usable: bool
    unusable_reason: str | None
    # Likeness to the uploaded photo, as Look.face_score.
    face_score: float | None
    created_at: str


class HairstyleOption(BaseModel):
    id: str
    name: str
    role: Role
    lengths: list[str]
    updo: bool


class MakeupColorOption(BaseModel):
    id: str
    name: str
    text: str


class StyleMakeup(BaseModel):
    primary: str
    alternates: list[str]
    needs_updo: bool


class LookDefault(BaseModel):
    hairstyle_id: str


class OutfitOption(BaseModel):
    id: str
    name: str
    bride: str
    groom: str
    headwear: str


class JewelryOption(BaseModel):
    id: str
    name: str
    text: str


class LookOptions(BaseModel):
    hairstyles: list[HairstyleOption]
    makeup_colors: list[MakeupColorOption]
    styles: dict[str, StyleMakeup]
    # The fixed outfits (V7.17), and which of them each style offers, the default first.
    outfits: list[OutfitOption]
    style_outfits: dict[str, list[str]]
    # V7.61: the bride's jewelry to choose from on a look, and the one each style suggests first.
    jewelry: list[JewelryOption]
    style_jewelry: dict[str, str]


class OutfitChoice(InputModel):
    outfit_id: str


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
    has_pending_face_scores: bool
    makeup_level: MakeupLevel
    groom_makeup_level: MakeupLevel
    bride_retouch_settings: RetouchSettings
    groom_retouch_settings: RetouchSettings
    bride_hair_length: BrideHairLength
    groom_hair_length: GroomHairLength
    person_a: Person
    person_b: Person
    previews: list[Slot]
    photo_sets: list[PhotoSet]
    photos: list[Slot]
    # Front look candidates of both people (V5.0), oldest first.
    looks: list[Look]
    # V7.24: the side looks new shots of these front looks use (one per look and angle, from the current side
    # photo), oldest first. Deleting one makes the next shot that needs it edit a new one.
    side_looks: list[SideLook]
    # Per style, the looks a new sample uses (V7.0): the bride's selected look for the style and the groom's
    # selected look; null where none is selected yet.
    style_looks: dict[str, dict[str, str | None]]
    # Per style, the fixed outfit a new sample wears (V7.17).
    style_outfits: dict[str, str]
    # The bride's makeup candidates of every style (V7.8), oldest first.
    makeups: list[Makeup]
    # Per person, the default candidates for their stated hair length.
    default_look_items: dict[str, list[LookDefault]]


class SideLook(BaseModel):
    """A side look (V5.0): a side photo edited into the same look as its front look. Since V7.30 the user makes it in
    the look step; a shot that needs one that is not ready still makes it then."""
    id: str
    parent_look_id: str
    person: Literal['a', 'b']
    role: Role
    angle: Literal['left', 'right']
    # V7.48: which side photo it was made from: 'left' / 'right' the half side, 'left_full' / 'right_full' the profile.
    facing: Literal['left', 'right', 'left_full', 'right_full']
    # V7.30: queued and running ones have no image yet; failed ones carry the reason.
    status: Literal['waiting', 'running', 'ready', 'failed', 'interrupted']
    error_message: str | None
    asset_id: str | None
    # The uploaded side photo it was made from.
    source_asset_id: str
    created_at: str
    # V7.45: likeness to that side photo; null until scored, or when no face was found or scoring is off.
    face_score: float | None = None


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
    # V7.12: the user paused it; its queued tasks are held until it continues.
    paused: bool


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
    regenerate_mode: Literal['same', 'identity_boost', 'change_pose', 'change_shot', 'face_swap', 'face_fix', 'hand_fix', 'wash', 'regional_edit'] | None
    source_version_id: str | None
    model: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class Moment(BaseModel):
    title: str
    shot_size: ShotSize
    orientation: Literal['portrait', 'landscape']
    bride_facing: Literal['front', 'left', 'right', 'left_full', 'right_full']
    groom_facing: Literal['front', 'left', 'right', 'left_full', 'right_full']
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
    bride_facing: Literal['front', 'left', 'right', 'left_full', 'right_full'] | None = None
    groom_facing: Literal['front', 'left', 'right', 'left_full', 'right_full'] | None = None
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


class FaceFixCreate(InputModel):
    client_request_id: str = Field(min_length=1)
    # The image model for the edits; the default model when omitted.
    model: str | None = None


class FaceSwapCreate(InputModel):
    client_request_id: str = Field(min_length=1)
