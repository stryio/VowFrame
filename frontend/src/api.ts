// Typed client for the backend contract in docs/API.md.

export type Role = "bride" | "groom";
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

export interface Person {
  asset_id: string | null;
  role: Role;
}

export interface VersionRef {
  id: string;
  asset_id: string;
  version_number: number;
  created_at: string;
  model: string | null;
  /** Face-swap versions point at the version they were swapped from (same composition). */
  source_version_id: string | null;
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
  person_a: Person;
  person_b: Person;
  previews: Slot[];
  photo_sets: PhotoSet[];
  photos: Slot[];
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
/** Everything that can produce a version; face_swap is local and free. */
export type VersionMode = SlotAction | "face_swap";
/** The five-step shot ladder; the only sizes new shots can use. */
export type ShotSize = "wide" | "full" | "medium" | "close" | "extreme_close";
/** Older works may still carry the retired studio labels and faceless detail shots. */
export type StoredShotSize = ShotSize | "detail" | "three_quarter" | "half";

export const SHOT_SIZES: ShotSize[] = ["wide", "full", "medium", "close", "extreme_close"];
export const SHOT_LABEL: Record<StoredShotSize, string> = {
  wide: "远景",
  full: "全景",
  medium: "中景",
  close: "近景",
  extreme_close: "特写",
  detail: "局部特写",
  three_quarter: "七分身",
  half: "半身",
};
/** Old detail shots show hands, rings or flowers only: nothing to face-swap or make more alike. */
export const hasFaces = (slot: Slot) => slot.shot_size !== "detail";

export type Orientation = "portrait" | "landscape";
export const ORIENTATIONS: Orientation[] = ["portrait", "landscape"];
export const ORIENTATION_LABEL: Record<Orientation, string> = { portrait: "竖版", landscape: "横版" };

/** The user picks the portrait ratio; landscape shots use the same ratio turned sideways. */
export type PortraitRatio = "3:4" | "2:3" | "9:16";

export interface Moment {
  title: string;
  shot_size: ShotSize;
  orientation: Orientation;
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
}

export interface Style {
  id: string;
  name: string;
  description: string;
}

export interface Model {
  id: string;
  name: string;
  family: "openai" | "gemini";
  max_reference_images: number;
  description: string;
}

export type BatchReferenceMode = "prompt_only" | "with_preview";

export interface Settings {
  default_model: string;
  batch_reference_mode: BatchReferenceMode;
  director_model: string | null;
  portrait_ratio: PortraitRatio;
  auto_face_swap: boolean;
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
  models: () => request<Model[]>("/models"),
  settings: () => request<Settings>("/settings"),
  updateSettings: (patch: Partial<Settings>) => request<Settings>("/settings", json("PATCH", patch)),

  listProjects: () => request<ProjectSummary[]>("/projects"),
  getProject: (id: string) => request<ProjectDetail>(`/projects/${id}`),
  createProject: (name?: string) =>
    request<ProjectDetail>("/projects", json("POST", name ? { name } : {})),
  updateProject: (id: string, patch: { name?: string; person_a_role?: Role }) =>
    request<ProjectDetail>(`/projects/${id}`, json("PATCH", patch)),
  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),

  uploadImage: (id: string, person: "a" | "b", file: File) => {
    const form = new FormData();
    form.append("person", person);
    form.append("file", file);
    return request<ProjectDetail>(`/projects/${id}/images`, { method: "POST", body: form });
  },

  createPreviews: (id: string, options: { styleIds?: string[]; model?: string } = {}) =>
    request<Batch>(
      `/projects/${id}/previews`,
      json("POST", {
        client_request_id: crypto.randomUUID(),
        style_ids: options.styleIds,
        model: options.model,
      }),
    ),
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

  listTasks: (params: { project_id?: string; batch_id?: string; status?: TaskStatus }) => {
    const query = new URLSearchParams(
      Object.entries(params).filter((e): e is [string, string] => Boolean(e[1])),
    );
    return request<Task[]>(`/tasks?${query}`);
  },
  retryTask: (id: string) => request<Task>(`/tasks/${id}/retry`, json("POST")),
  cancelTask: (id: string) => request<Task>(`/tasks/${id}/cancel`, json("POST")),
};

export const ACTIVE_STATUSES: TaskStatus[] = ["waiting", "running"];

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  waiting: "等待中",
  held: "已搁置",
  running: "生成中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

export const BATCH_STATUS_LABEL: Record<BatchStatus, string> = {
  running: "进行中",
  held: "已搁置",
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
};

/** The current version is a face swap whose source can be compared in place. */
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
