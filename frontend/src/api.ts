// Typed client for the backend contract in docs/API.md.

export type Role = "bride" | "groom";
export type MakeupLevel = "light" | "natural" | "refined";
export type AdjustmentLevel = "off" | "minimal" | "subtle" | "light" | "mild" | "balanced" | "moderate" | "enhanced" | "strong" | "intense" | "maximum";
export type ConcealingLevel = Exclude<AdjustmentLevel, "balanced" | "maximum"> | "standard" | "full";
export interface RetouchSettings {
  slimming: AdjustmentLevel;
  brightening: AdjustmentLevel;
  concealing: ConcealingLevel;
  chin: AdjustmentLevel;
  nasolabial: AdjustmentLevel;
}
export type BrideHairLength = "long" | "medium" | "short";
export type GroomHairLength = "buzz" | "short" | "medium";
export type Stage = "upload" | "style" | "shooting";
export type TaskStatus =
  | "waiting"
  | "held"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";
export type BatchStatus = "running" | "held" | "needs_attention" | "completed";
export type BatchType = "preview" | "photo" | "regenerate";

export interface HandRegion {
  label: string;
  status: "repair" | "reconstruct" | "uncertain";
  /** Normalized coordinates in the EXIF-oriented full image. */
  bbox: [number, number, number, number];
  evidence: string[];
  problems: string[];
  goal: string;
  preserve: string[];
  uncertainties: string[];
}

export interface HandAnalysis {
  status: "repair" | "reconstruct" | "normal" | "uncertain";
  summary: string;
  regions: HandRegion[];
  source_version_id: string;
  scope: "detect" | "selection";
  model: string;
  provider: ImageProviderId;
  image_size: [number, number];
  annotation_sha256: string | null;
  annotation_kind: "crop" | "reference" | "markup" | "mask" | null;
  created_at: string;
  selected_region_index: number | null;
  repair_prompts: string[];
}

export interface Person {
  asset_id: string | null;
  /** The half side photos (about 45°). */
  left_asset_id: string | null;
  right_asset_id: string | null;
  /** V7.48: the full side photos (profiles, about 90°). */
  left_full_asset_id: string | null;
  right_full_asset_id: string | null;
  role: Role;
}

/** Similarity to each person's uploaded front photo (FaceFusion's ArcFace); null when that face was not found. */
export interface FaceScores {
  bride: number | null;
  groom: number | null;
}

export interface VersionRef {
  id: string;
  asset_id: string;
  version_number: number;
  created_at: string;
  model: string | null;
  /** Face-swap versions point at the version they were swapped from (same composition). */
  source_version_id: string | null;
  /** V7.25: how this version was made; null for a first generation. */
  regenerate_mode?: VersionMode | null;
  /** Null until measured (and for pictures without faces to measure). */
  face_scores?: FaceScores | null;
  /** The service returned another size than requested. */
  size_warning?: string | null;
}

export interface TaskBrief {
  id: string;
  status: TaskStatus;
  error_code: string | null;
  error_message: string | null;
  /** Model chosen for this attempt; differs from current_version.model after a failed retry. */
  model: string | null;
  regenerate_mode: VersionMode | null;
}

export interface Slot {
  id: string;
  kind: "preview" | "photo";
  batch_id: string;
  style_id: string;
  style_name: string;
  pose_id: string | null;
  pose_name: string | null;
  shot_size: StoredShotSize | null;
  current_version: VersionRef | null;
  version_count: number;
  latest_task: TaskBrief | null;
  /** Current version was generated from photos that have since been replaced. */
  is_stale: boolean;
  /** The couple's styled photos the current version was shot from (V4.0); null for older pictures. */
  styled_asset_ids: { bride: string; groom: string } | null;
  /** The looks the current version was shot from (V5.0); null for older pictures. */
  look_ids: LookPair | null;
  /** The room the current version was shot in (V7.34); null for styles without rooms and older pictures. */
  room?: ShotRoom | null;
  /** The style art (outfit in the scene) the current version was shot from (V7.35); null without one. */
  style_art_asset_id?: string | null;
}

export interface ShotRoom {
  id: string;
  name: string;
  /** 1 is the room's main picture, where samples are shot. */
  area: number | null;
  area_name: string | null;
}

/** A room a style is shot in (V7.34): the user picks it for a sample; its set is spread over the room's areas. */
export interface Room {
  id: string;
  name: string;
  /** image_url is null while an area other than the main one is not made yet (V7.38). */
  areas: { name: string; image_url: string | null }[];
}

export interface LookPair {
  bride: string;
  groom: string;
}

export type LookStatus = "waiting" | "running" | "ready" | "failed" | "interrupted";

/** A styled photo candidate (V5.0): hair and makeup in one edit; a bride look is made for one style (V7.0). */
export interface Look {
  id: string;
  person: "a" | "b";
  role: Role;
  /** The style a bride look was made for; null for the groom's (shared by every style) and older looks. */
  style_id: string | null;
  hairstyle_id: string;
  hairstyle_name: string;
  /** V7.61: the earrings and necklace the bride wears on this look; null for none. */
  jewelry_id: string | null;
  jewelry_name: string | null;
  makeup_color: string | null;
  makeup_color_name: string | null;
  makeup_level: MakeupLevel;
  hair_length: string;
  retouch_settings: RetouchSettings;
  status: LookStatus;
  error_code: string | null;
  error_message: string | null;
  asset_id: string | null;
  source_asset_id: string;
  selected: boolean;
  selected_order: number | null;
  usable: boolean;
  unusable_reason: string | null;
  face_score: number | null;
  created_at: string;
  /** V7.5: the bride's made-up photo before the hair edit, and its likeness; null for one-edit looks. */
  makeup_asset_id: string | null;
  makeup_face_score: number | null;
  /** "user" when the user picked the selected look, "auto" when it was picked as the most similar. */
  selected_by: "user" | "auto" | null;
  /** V7.8: the makeup candidate the hair was done on; null for the groom's and older looks. */
  makeup_id: string | null;
  /** V7.21: the style whose colour family and finish the bride's makeup was painted in; null for the groom's. */
  makeup_style_id: string | null;
}

/** A side photo edited into the same look as its front look (V5.0), made when a shot first needs that angle. */
export interface SideLook {
  id: string;
  parent_look_id: string;
  person: "a" | "b";
  role: Role;
  angle: "left" | "right";
  /** V7.48: the side photo it was made from: half side ("left"/"right") or profile ("left_full"/"right_full"). */
  facing: SideFacing;
  /** V7.30: made in the look step on request; queued and running ones have no image yet. */
  status: LookStatus;
  error_message: string | null;
  asset_id: string | null;
  /** The uploaded side photo it was made from. */
  source_asset_id: string;
  created_at: string;
  /** V7.45: likeness to that side photo; null until scored or when no face was found. */
  face_score: number | null;
}

/** The bride's makeup for one style, hair unchanged (V7.8); new bride looks of the style use the selected one. */
export interface Makeup {
  id: string;
  person: "a" | "b";
  /** V7.22: the groom's retouched photo has no style or colour and serves every style. */
  role: Role;
  style_id: string | null;
  style_name: string | null;
  /** V7.21: the style whose colour family and finish it was painted in; style_id unless reused from another style. */
  makeup_style_id: string | null;
  makeup_style_name: string | null;
  makeup_color: string | null;
  makeup_color_name: string | null;
  makeup_level: MakeupLevel;
  status: LookStatus;
  retouch_settings: RetouchSettings;
  error_code: string | null;
  error_message: string | null;
  asset_id: string | null;
  source_asset_id: string;
  selected: boolean;
  /** "user" when the user picked it, "auto" when it was picked as the most similar; null when not selected. */
  selected_by: "user" | "auto" | null;
  usable: boolean;
  unusable_reason: string | null;
  face_score: number | null;
  created_at: string;
}

export interface LookItem {
  hairstyle_id: string;
  /** V7.61: the bride's earrings and necklace; none when omitted. */
  jewelry_id?: string | null;
}

export interface LookOptions {
  hairstyles: { id: string; name: string; role: Role; lengths: string[]; updo: boolean }[];
  makeup_colors: { id: string; name: string; text: string }[];
  styles: Record<string, { primary: string; alternates: string[]; needs_updo: boolean }>;
  /** The fixed outfits (V7.17), and which of them each style offers, the default first. */
  outfits: { id: string; name: string; bride: string; groom: string; headwear: string }[];
  style_outfits: Record<string, string[]>;
  /** V7.61: the bride's jewelry to choose from on a look, and the one each style suggests first. */
  jewelry: { id: string; name: string; text: string }[];
  style_jewelry: Record<string, string>;
}

export interface PhotoSet {
  style_id: string;
  style_name: string;
  slot_count: number;
  completed_count: number;
  batch_ids: string[];
}

export interface ProjectSummary {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  stage: Stage;
  cover_asset_id: string | null;
  photo_count: number;
  active_task_count: number;
  has_pending_recovery: boolean;
}

export interface ProjectDetail extends ProjectSummary {
  /** Background similarity scoring continues after image generation completes. */
  has_pending_face_scores: boolean;
  makeup_level: MakeupLevel;
  groom_makeup_level: MakeupLevel;
  bride_retouch_settings: RetouchSettings;
  groom_retouch_settings: RetouchSettings;
  bride_hair_length: BrideHairLength;
  groom_hair_length: GroomHairLength;
  person_a: Person;
  person_b: Person;
  previews: Slot[];
  photo_sets: PhotoSet[];
  photos: Slot[];
  /** Front look candidates of both people (V5.0), oldest first. */
  looks: Look[];
  /** V7.24: the side looks new shots of these front looks use; deleting one makes the next shot edit a new one. */
  side_looks: SideLook[];
  /** Per style, the looks a new sample uses (V7.0): the bride's selected look for it and the groom's selected look. */
  style_looks: Record<string, { bride: string | null; groom: string | null }>;
  /** Per style, the fixed outfit a new sample wears (V7.17). */
  style_outfits: Record<string, string>;
  /** The bride's makeup candidates of every style (V7.8), oldest first. */
  makeups: Makeup[];
  /** Per role, the default candidates for the stated hair length. */
  default_look_items: Record<Role, LookItem[]>;
}

export interface Batch {
  id: string;
  project_id: string;
  batch_type: BatchType;
  style_id: string | null;
  style_name: string | null;
  created_at: string;
  status: BatchStatus;
  total: number;
  counts: Record<TaskStatus, number>;
  /** V7.12: paused by the user; its queued tasks wait until it continues. */
  paused: boolean;
}

export interface Task {
  id: string;
  project_id: string;
  batch_id: string;
  target_slot_id: string;
  status: TaskStatus;
  style_id: string | null;
  pose_id: string | null;
  prompt: string;
  error_code: string | null;
  error_message: string | null;
  auto_retry_count: number;
  retry_of_task_id: string | null;
  regenerate_mode: VersionMode | null;
  source_version_id: string | null;
  model: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export type RegenerateMode = "same" | "identity_boost";
/** Paid actions: they go through the model / cost confirmation dialog. */
export type SlotAction = RegenerateMode | "change_pose" | "change_shot";
/** Everything that can produce a version; face_swap is local and free, face_fix and hand_fix are one paid edit each. */
export type VersionMode = SlotAction | "face_swap" | "face_fix" | "hand_fix" | "wash" | "regional_edit";
/** What follows each generated picture with faces; "split" fixes the bride by AI, then swaps the groom locally. */
export type AutoFaceFix = "split" | "ai" | "facefusion" | "off";
/** V7.29: what one person gets after a picture is generated. */
export type FaceFixMethod = "ai" | "facefusion" | "off";
/** Wide plus the four studio framings (V3.14); the only sizes new shots can use. */
export type ShotSize = "wide" | "full" | "three_quarter" | "half" | "close";
/** Works from V3.4–V3.11 may still carry retired sizes and faceless detail shots. */
export type StoredShotSize = ShotSize | "medium" | "extreme_close" | "detail";

export const SHOT_SIZES: ShotSize[] = ["wide", "full", "three_quarter", "half", "close"];
export const SHOT_LABEL: Record<StoredShotSize, string> = {
  wide: "远景",
  full: "全身",
  three_quarter: "七分身",
  half: "半身",
  close: "近景",
  medium: "中景",
  extreme_close: "特写",
  detail: "局部特写",
};
/** Old detail shots show hands, rings or flowers only: nothing to face-swap or make more alike. */
export const hasFaces = (slot: Slot) => slot.shot_size !== "detail";

export type Orientation = "portrait" | "landscape";
export type Facing = "front" | "left" | "right" | "left_full" | "right_full";
export type SideFacing = Exclude<Facing, "front">;
/** V7.48: the side photo slots, half sides first. */
export const SIDE_FACINGS: SideFacing[] = ["left", "right", "left_full", "right_full"];
export const FACING_NAMES: Record<Facing, string> = {
  front: "正面",
  left: "朝左半侧",
  right: "朝右半侧",
  left_full: "朝左正侧面",
  right_full: "朝右正侧面",
};
export const ORIENTATIONS: Orientation[] = ["portrait", "landscape"];
export const ORIENTATION_LABEL: Record<Orientation, string> = { portrait: "竖版", landscape: "横版" };

/** The user picks the portrait ratio; landscape shots use the same ratio turned sideways. */
export type PortraitRatio = "3:4" | "2:3" | "9:16";

export interface Moment {
  title: string;
  shot_size: ShotSize;
  orientation: Orientation;
  bride_facing: Facing;
  groom_facing: Facing;
  text: string;
}

export interface MomentDraft {
  id: string;
  project_id: string;
  preview_slot_id: string;
  style_id: string;
  style_name: string;
  director_model: string | null;
  moments: Moment[];
  used_by_batch_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface VersionDetail {
  id: string;
  asset_id: string;
  version_number: number;
  created_at: string;
  model: string | null;
  pose_id: string | null;
  pose_name: string | null;
  regenerate_mode: VersionMode | null;
  source_version_id: string | null;
  is_current: boolean;
  face_scores?: FaceScores | null;
  /** The service returned another size than requested. */
  size_warning?: string | null;
}

export interface Style {
  id: string;
  name: string;
  description: string;
  /** Generated when the user picks no style before the first samples. */
  default_preview: boolean;
  /** Hidden from new picks; still shown where a project already has its sample. */
  retired: boolean;
  /** V7.34: the rooms a sample can be shot in; empty for styles without rooms. */
  rooms: Room[];
}

export interface Model {
  id: string;
  name: string;
  family: "openai" | "gemini" | "seedream";
  max_reference_images: number;
  description: string;
  /** The image services offering it (V7.11). */
  providers: ImageProviderId[];
}

export type ImageProviderId = "88api" | "pptoken" | "ark";

/** Steps with their own image service and model in Settings ("face_fix" is the hand fix; "face" the AI face fix). */
export type StageKind = "makeup" | "look" | "style_art" | "face_fix" | "face" | "wash" | "regional";
export type StageChoice = { model: string | null; provider: ImageProviderId | null };

export interface ImageProvider {
  id: ImageProviderId;
  name: string;
  available: boolean;
  /** Why it cannot be selected, written for the user. */
  reason: string | null;
}

export type BatchReferenceMode = "prompt_only" | "with_preview";

export interface Settings {
  default_model: string;
  batch_reference_mode: BatchReferenceMode;
  director_model: string | null;
  portrait_ratio: PortraitRatio;
  /** The preset the two people's methods make, or "custom" (V7.29); PATCH may still send a preset. */
  auto_face_fix: AutoFaceFix | "custom";
  auto_face_fix_bride: FaceFixMethod;
  auto_face_fix_groom: FaceFixMethod;
  /** The hand fix's model ("face_fix" predates V7.32); null follows default_model. */
  face_fix_model: string | null;
  /** V7.26: the wash's image model; null follows the default model. */
  wash_model: string | null;
  /** V7.27: the image service of hand fixes and of washes; null follows image_provider. */
  face_fix_provider: ImageProviderId | null;
  wash_provider: ImageProviderId | null;
  /** 2026-11-13: the other steps' own service and model (see StageKind); null follows image_provider / default_model. */
  face_model: string | null;
  face_provider: ImageProviderId | null;
  regional_model: string | null;
  regional_provider: ImageProviderId | null;
  makeup_model: string | null;
  makeup_provider: ImageProviderId | null;
  look_model: string | null;
  look_provider: ImageProviderId | null;
  style_art_model: string | null;
  style_art_provider: ImageProviderId | null;
  /** V7.11: the image service models both services offer go to. */
  image_provider: ImageProviderId;
  image_providers: ImageProvider[];
  /** The service the AI director uses; director_model is that service's model. */
  director_provider: ImageProviderId;
  director_providers: ImageProvider[];
  /** V7.33: whether new shots of a live style copy the clothes and the place from its style art. */
  style_art_mode: "on" | "off";
}

/** V7.33: a live style's art, its outfit worn by no one in its main scene; shared by every project. */
export interface StyleArt {
  style_id: string;
  style_name: string;
  outfit_id: string;
  outfit_name: string;
  /** V7.34: the room this art shows (V7.50: one art per room, of its main picture, area 1); null for a style's
   * single main-scene art. */
  room_id?: string | null;
  room_name?: string | null;
  area?: number | null;
  area_name?: string | null;
  area_image_url?: string | null;
  /** The latest attempt; null fields while the style has none (it is made when a shot first needs it). */
  id: string | null;
  status: "running" | "ready" | "failed" | "interrupted" | null;
  model: string | null;
  error_message: string | null;
  created_at: string | null;
  finished_at: string | null;
  /** The pictures of the art shots use, or of the latest attempt while there is none. */
  product_url: string | null;
  scene_url: string | null;
  image_url: string | null;
  /** The art new shots use; differs from id while a new one is made or after one failed. */
  in_use_id: string | null;
  /** Paid edits that making it (again) costs now (V7.35). */
  cost: number;
}

export interface FaceSwapStatus {
  available: boolean;
  mode: "facefusion" | "mock" | null;
  /** Why it is unavailable, written for the user. */
  reason: string | null;
}

export interface Health {
  ok: boolean;
  generator: string;
  model: string | null;
  face_swap: FaceSwapStatus;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiError(0, "network", "无法连接本地服务，请确认后端已启动");
  }
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body?.detail;
    const message =
      typeof detail?.message === "string" ? detail.message : `请求失败（${res.status}）`;
    throw new ApiError(res.status, detail?.code ?? "unknown", message);
  }
  return body as T;
}

function json(method: string, data?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  };
}

export const assetUrl = (assetId: string) => `/api/assets/${assetId}`;

export const api = {
  health: () => request<Health>("/health"),
  styles: () => request<Style[]>("/styles"),
  /** The models of an image service (V7.27), or of the one Settings selects. */
  models: (provider?: ImageProviderId) => request<Model[]>(provider ? `/models?provider=${provider}` : "/models"),
  regionalEditModels: (provider: ImageProviderId) => request<Model[]>(`/regional-edit/models?provider=${provider}`),
  regionalEditOptions: (provider: ImageProviderId) => request<{
    models: Model[];
    /** models: the ids that can do this method (Nano Banana 2.1 only "crop"). */
    methods: { id: "crop" | "reference" | "markup" | "mask"; name: string; available: boolean; description: string; models: string[] }[];
  }>(`/regional-edit/options?provider=${provider}`),
  regionalEdit: (slotId: string, data: FormData) => request<Batch>(`/slots/${slotId}/regional-edit`, { method: "POST", body: data }),
  analyzeHands: (slotId: string, data: FormData) => request<HandAnalysis>(`/slots/${slotId}/hand-analysis`, { method: "POST", body: data }),
  settings: () => request<Settings>("/settings"),
  updateSettings: (patch: Partial<Settings>) => request<Settings>("/settings", json("PATCH", patch)),
  styleArts: () => request<StyleArt[]>("/style-arts"),
  /** Make a room area's picture again from the room's main picture: one paid edit (V7.38). */
  /** The arts a set from this sample uses (V7.35): one per area of its room, in its outfit. */
  slotStyleArts: (slotId: string) => request<StyleArt[]>(`/slots/${slotId}/style-arts`),
  /** Make a style's art (again) with the default model; costs StyleArt.cost paid edits (V7.33–V7.35). */
  generateStyleArt: (styleId: string, options: { room?: { id: string; area: number }; outfit?: string } = {}) => {
    const query = new URLSearchParams();
    if (options.room) {
      query.set("room", options.room.id);
      query.set("area", String(options.room.area));
    }
    if (options.outfit) query.set("outfit", options.outfit);
    const suffix = query.toString();
    return request<StyleArt>(`/style-arts/${styleId}/generate${suffix ? `?${suffix}` : ""}`, json("POST"));
  },

  listProjects: () => request<ProjectSummary[]>("/projects"),
  getProject: (id: string) => request<ProjectDetail>(`/projects/${id}`),
  createProject: (name?: string) =>
    request<ProjectDetail>("/projects", json("POST", name ? { name } : {})),
  updateProject: (id: string, patch: {
    name?: string;
    person_a_role?: Role;
    makeup_level?: MakeupLevel;
    groom_makeup_level?: MakeupLevel;
    bride_retouch_settings?: RetouchSettings;
    groom_retouch_settings?: RetouchSettings;
    bride_hair_length?: BrideHairLength;
    groom_hair_length?: GroomHairLength;
  }) =>
    request<ProjectDetail>(`/projects/${id}`, json("PATCH", patch)),
  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),

  uploadImage: (id: string, person: "a" | "b", file: File, angle: Facing = "front") => {
    const form = new FormData();
    form.append("person", person);
    form.append("angle", angle);
    form.append("file", file);
    return request<ProjectDetail>(`/projects/${id}/images`, { method: "POST", body: form });
  },
  deleteSideImage: (id: string, person: "a" | "b", angle: SideFacing) =>
    request<void>(`/projects/${id}/images/${person}/${angle}`, { method: "DELETE" }),

  /** rooms: the room each style with rooms is shot in (V7.34); a style left out takes its first room. */
  createPreviews: (
    id: string,
    options: { styleIds: string[]; model?: string; rooms?: Record<string, string[]>; outfits?: Record<string, string> },
  ) =>
    request<Batch>(
      `/projects/${id}/previews`,
      json("POST", {
        client_request_id: crypto.randomUUID(),
        style_ids: options.styleIds,
        model: options.model,
        // V7.56: one sample per picked room of a style.
        rooms: options.rooms,
        // V7.37: the outfit picked with the room; it also becomes the project's choice for the style.
        outfits: options.outfits,
      }),
    ),
  lookOptions: () => request<LookOptions>("/look-options"),
  chooseStyleOutfit: (projectId: string, styleId: string, outfitId: string) =>
    request<ProjectDetail>(`/projects/${projectId}/style-outfits/${styleId}`, json("PUT", { outfit_id: outfitId })),
  /** styleId: the style a bride look is made for; omitted for the groom (V7.0). */
  createLooks: (projectId: string, person: "a" | "b", items: LookItem[], styleId?: string) =>
    request<Look[]>(
      `/projects/${projectId}/looks`,
      json("POST", { client_request_id: crypto.randomUUID(), person, style_id: styleId, items }),
    ),
  retryLook: (lookId: string) => request<Look>(`/looks/${lookId}/retry`, { method: "POST" }),
  selectLook: (lookId: string) => request<Look>(`/looks/${lookId}/select`, { method: "POST" }),
  unselectLook: (lookId: string) => request<Look>(`/looks/${lookId}/unselect`, { method: "POST" }),
  deleteLook: (lookId: string) => request<void>(`/looks/${lookId}`, { method: "DELETE" }),
  deleteSideLook: (lookId: string) => request<void>(`/side-looks/${lookId}`, { method: "DELETE" }),
  makeSideLooks: (lookId: string) => request<SideLook[]>(`/looks/${lookId}/side-looks`, { method: "POST" }),
  /** count makeup candidates of the bride for one style, one paid edit each (V7.8). */
  /** styleId null: the groom's retouched photos (V7.22), shared by every style. */
  createMakeups: (projectId: string, person: "a" | "b", styleId: string | null, count: number) =>
    request<Makeup[]>(
      `/projects/${projectId}/makeups`,
      json("POST", { client_request_id: crypto.randomUUID(), person, style_id: styleId, count }),
    ),
  retryMakeup: (makeupId: string) => request<Makeup>(`/makeups/${makeupId}/retry`, { method: "POST" }),
  selectMakeup: (makeupId: string) => request<Makeup>(`/makeups/${makeupId}/select`, { method: "POST" }),
  deleteMakeup: (makeupId: string) => request<void>(`/makeups/${makeupId}`, { method: "DELETE" }),
  /** Use a ready makeup of another style for this style too, with its hairstyle photos; no new edit (V7.21). */
  reuseMakeup: (makeupId: string, styleId: string) =>
    request<Makeup>(`/makeups/${makeupId}/reuse`, json("POST", { style_id: styleId })),
  createDraft: (projectId: string, previewSlotId: string, count: number) =>
    request<MomentDraft>(
      `/projects/${projectId}/moment-drafts`,
      json("POST", { preview_slot_id: previewSlotId, count }),
    ),
  updateMoment: (draftId: string, index: number, patch: Partial<Moment>) =>
    request<MomentDraft>(`/moment-drafts/${draftId}/moments/${index}`, json("PATCH", patch)),
  /** Without shotSize: a brand-new moment. With shotSize: same action at another shot size. */
  rewriteMoment: (draftId: string, index: number, shotSize?: ShotSize) =>
    request<MomentDraft>(
      `/moment-drafts/${draftId}/moments/${index}/rewrite`,
      json("POST", shotSize ? { shot_size: shotSize } : {}),
    ),
  createPhotoBatch: (projectId: string, draftId: string, model?: string) =>
    request<Batch>(
      `/projects/${projectId}/photo-batches`,
      json("POST", { client_request_id: crypto.randomUUID(), draft_id: draftId, model }),
    ),

  regenerateSlot: (slotId: string, mode: RegenerateMode, model?: string) =>
    request<Batch>(
      `/slots/${slotId}/regenerate`,
      json("POST", { client_request_id: crypto.randomUUID(), mode, model }),
    ),
  changePose: (slotId: string, model?: string) =>
    request<Batch>(`/slots/${slotId}/change-pose`, json("POST", { client_request_id: crypto.randomUUID(), model })),
  changeShot: (slotId: string, shotSize: ShotSize, model?: string) =>
    request<Batch>(
      `/slots/${slotId}/change-shot`,
      json("POST", { client_request_id: crypto.randomUUID(), shot_size: shotSize, model }),
    ),
  batchAction: (slotIds: string[], action: SlotAction, model?: string, shotSize?: ShotSize) =>
    request<Batch>(
      "/slots/batch-action",
      json("POST", {
        client_request_id: crypto.randomUUID(),
        slot_ids: slotIds,
        action,
        model,
        shot_size: shotSize,
      }),
    ),
  faceSwap: (slotId: string) =>
    request<Batch>(`/slots/${slotId}/face-swap`, json("POST", { client_request_id: crypto.randomUUID() })),
  faceFix: (slotId: string, model: string) =>
    request<Batch>(`/slots/${slotId}/face-fix`, json("POST", { client_request_id: crypto.randomUUID(), model })),
  wash: (slotId: string, model: string) =>
    request<Batch>(`/slots/${slotId}/wash`, json("POST", { client_request_id: crypto.randomUUID(), model })),
  handFix: (slotId: string, model: string) =>
    request<Batch>(`/slots/${slotId}/hand-fix`, json("POST", { client_request_id: crypto.randomUUID(), model })),
  batchFaceFix: (slotIds: string[], model: string) =>
    request<Batch>(
      "/slots/batch-action",
      json("POST", { client_request_id: crypto.randomUUID(), slot_ids: slotIds, action: "face_fix", model }),
    ),
  batchFaceSwap: (slotIds: string[]) =>
    request<Batch>(
      "/slots/batch-action",
      json("POST", { client_request_id: crypto.randomUUID(), slot_ids: slotIds, action: "face_swap" }),
    ),
  versions: (slotId: string) => request<VersionDetail[]>(`/slots/${slotId}/versions`),
  restoreVersion: (slotId: string, versionId: string) =>
    request<Slot>(`/slots/${slotId}/restore`, json("POST", { version_id: versionId })),
  deleteSlot: (slotId: string) => request<void>(`/slots/${slotId}`, { method: "DELETE" }),
  /** Downloads a ZIP of current versions; resolves after the browser save starts. */
  exportZip: async (projectId: string, slotIds?: string[]) => {
    let res: Response;
    try {
      res = await fetch(`/api/projects/${projectId}/export`, json("POST", { slot_ids: slotIds }));
    } catch {
      throw new ApiError(0, "network", "无法连接本地服务，请确认后端已启动");
    }
    if (!res.ok) {
      const detail = (await res.json().catch(() => null))?.detail;
      throw new ApiError(res.status, detail?.code ?? "unknown", detail?.message ?? `导出失败（${res.status}）`);
    }
    const blob = await res.blob();
    const name = /filename\*=UTF-8''([^;]+)/.exec(res.headers.get("content-disposition") ?? "")?.[1];
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name ? decodeURIComponent(name) : "vowframe.zip";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  },

  listBatches: (projectId: string) => request<Batch[]>(`/projects/${projectId}/batches`),
  continueBatch: (id: string) => request<Batch>(`/batches/${id}/continue`, json("POST")),
  cancelBatch: (id: string) => request<Batch>(`/batches/${id}/cancel`, json("POST")),
  pauseBatch: (id: string) => request<Batch>(`/batches/${id}/pause`, json("POST")),
  /** Cancels the queue and stops the tasks being generated; their results are discarded (V7.12). */
  stopBatch: (id: string) => request<Batch>(`/batches/${id}/stop`, json("POST")),

  listTasks: (params: { project_id?: string; batch_id?: string; target_slot_id?: string; status?: TaskStatus }) => {
    const query = new URLSearchParams(
      Object.entries(params).filter((e): e is [string, string] => Boolean(e[1])),
    );
    return request<Task[]>(`/tasks?${query}`);
  },
  retryTask: (id: string) => request<Task>(`/tasks/${id}/retry`, json("POST")),
  cancelTask: (id: string, expectedStatus?: TaskStatus) =>
    request<Task>(`/tasks/${id}/cancel${expectedStatus ? `?expected_status=${expectedStatus}` : ""}`, json("POST")),
  pauseTask: (id: string) => request<Task>(`/tasks/${id}/pause`, json("POST")),
  continueTask: (id: string) => request<Task>(`/tasks/${id}/continue`, json("POST")),
};

export const ACTIVE_STATUSES: TaskStatus[] = ["waiting", "running"];

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  waiting: "等待中",
  held: "已暂停",
  running: "生成中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

export const BATCH_STATUS_LABEL: Record<BatchStatus, string> = {
  running: "进行中",
  held: "已暂停",
  needs_attention: "需处理",
  completed: "已完成",
};

export const SLOT_ACTION_LABEL: Record<SlotAction, string> = {
  same: "重新生成",
  identity_boost: "加强长相重试",
  change_pose: "换动作",
  change_shot: "换景别",
};

export const VERSION_MODE_LABEL: Record<VersionMode, string> = {
  ...SLOT_ACTION_LABEL,
  face_swap: "换脸修正",
  face_fix: "AI 修脸",
  hand_fix: "修手",
  wash: "洗图",
  regional_edit: "局部编辑",
};

/** What the fix chip on a picture says: the current version corrects faces or hands of the version it came from. */
export const fixLabel = (slot: Slot) => {
  const mode = slot.current_version?.regenerate_mode;
  return mode === "regional_edit" ? "局部编辑" : mode === "hand_fix" ? "修手" : mode === "wash" ? "洗图" : "修脸";
};

/** The current version is a face swap, AI face fix or hand fix whose source can be compared in place. */
export const isFaceSwapped = (slot: Slot) => Boolean(slot.current_version?.source_version_id);

export const BATCH_TYPE_LABEL: Record<BatchType, string> = {
  preview: "风格样片",
  photo: "拍一组",
  regenerate: "重新生成",
};

export const isGenerating = (slot: Slot) =>
  slot.latest_task?.status === "waiting" || slot.latest_task?.status === "running";

export const isBroken = (slot: Slot) =>
  slot.latest_task?.status === "failed" || slot.latest_task?.status === "interrupted";
