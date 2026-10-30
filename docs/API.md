# VowFrame 后端 API 约定（骨架阶段）

前后端共同遵守的接口契约。实现与本文不一致时，以修改本文为先。

- 基础路径：`/api`
- 数据格式：JSON（上传为 `multipart/form-data`）
- 时间：ISO 8601 字符串（UTC）
- ID：字符串（UUID4 hex）
- 错误：HTTP 4xx/5xx，响应体 `{"detail": {"code": "<snake_case>", "message": "<给用户看的中文>"}}`

本阶段范围：项目、上传、风格样片、按风格拍组（批量生成）、相册（单张/多选重生、加强长相重试、换动作、换景别、换脸修正、历史版本与恢复、删除、ZIP 下载）、任务与批次控制。

**一个项目可以拍多组风格**：试拍的每张样片都可以「用这个风格拍一组」，次数不限；不存在「项目唯一选中风格」。

---

## 1. 数据类型

### Role

`"bride" | "groom"`。项目只存 `person_a_role`，人物 B 的角色永远与 A 相反（避免两人角色相同的非法状态）。

### Stage（由后端计算，不存储）

| 值 | 条件 |
|---|---|
| `upload` | 人物 A 或 B 照片缺失 |
| `style` | 照片齐全，但还没有拍任何一组 |
| `shooting` | 至少拍了一组（存在 `kind = "photo"` 的作品位置） |

### TaskStatus

`waiting | held | running | completed | failed | cancelled | interrupted`，含义见 PRD 6.2。

### BatchStatus（由任务状态汇总，不存储）

| 值 | 条件 |
|---|---|
| `running` | 存在 `waiting` 或 `running` |
| `held` | 否则，存在 `held` |
| `needs_attention` | 否则，存在 `failed` 或 `interrupted` |
| `completed` | 其他 |

### Person

```json
{ "asset_id": "…" | null, "left_asset_id": "…" | null, "right_asset_id": "…" | null, "role": "bride" }
```

`left_asset_id` / `right_asset_id` 是可选的侧面参考照；方向均按图片所见（`left` = 脸朝画面左侧）。

### VersionRef

```json
{ "id": "…", "asset_id": "…", "version_number": 1, "created_at": "…", "model": "gpt-image-2" | null, "source_version_id": "…" | null }
```

- `model`：生成该版本的任务所用模型（取自任务的模型配置快照）；换脸版本为两人实际所用模型「新娘/新郎」，如 `hyperswap_1c_256/hyperswap_1b_256`（2026-10-28 之前的换脸版本为 `hyperswap_1a_256/hyperswap_1c_256`）；模拟生成或无法确定时为 null。既有版本仍按其创建时的快照显示。
- `source_version_id`：换脸版本的来源版本（换脸时输入的那个版本）；其他版本为 null。

### TaskBrief

```json
{ "id": "…", "status": "running", "error_code": null, "error_message": null, "model": "gpt-image-2.5-flare" | null, "regenerate_mode": "face_swap" | null }
```

`regenerate_mode`：同 Task.regenerate_mode。界面据此区分「换脸修正中 / 换脸失败」与「重新生成中 / 生成失败」，因为两者的提示和费用含义不同。

`model`：该任务创建时选定的模型（与 `Task.model` 相同）。界面用它显示「这次尝试用的是哪个模型」，因为失败时 `current_version` 仍是旧版本、旧模型。

### Slot（作品位置：样片 `preview` 或相册作品 `photo`）

```json
{
  "id": "…",
  "kind": "photo",
  "batch_id": "…",
  "style_id": "korean_minimal",
  "style_name": "韩系极简",
  "pose_id": "pose_03" | null,
  "pose_name": "新郎从背后拥抱新娘" | null,
  "shot_size": "full" | null,
  "current_version": VersionRef | null,
  "version_count": 1,
  "latest_task": TaskBrief | null,
  "is_stale": false
}
```

- `latest_task`：以该位置为目标、创建时间最新且未被其他任务替代的任务。
- `pose_id`：旧版固定动作的编号；AI 导演写的瞬间为 null。
- `pose_name`：相册中显示的动作名称。AI 导演写的瞬间为其 `title`，旧版固定动作为动作名；样片为 null。均取自生成当前版本的任务（无版本时取最新任务）。
- `shot_size`：`"wide" | "full" | "three_quarter" | "half" | "close" | "medium" | "extreme_close" | "detail" | null`，同样取自对应任务；旧版固定动作与样片为 null。`wide`（远景）自 V3.14 起重新启用；`medium`（中景）、`extreme_close`（特写）、`detail`（局部特写）是 V3.4～V3.11 的停用景别，只会出现在旧作品上。
- `batch_id`：创建该位置的批次。
- `is_stale`（由后端计算）：当前版本是用旧照片生成的，即生成它的任务的正面照或所用侧面照已不再是项目当前对应照片。样片参考图不参与侧照 stale 判断。没有当前版本时为 false。

### PhotoSet（按风格汇总的「一组」，由后端计算）

```json
{
  "style_id": "korean_minimal",
  "style_name": "韩系极简",
  "slot_count": 16,
  "completed_count": 12,
  "batch_ids": ["…", "…"]
}
```

同一风格拍多次，汇总为同一个 PhotoSet；`batch_ids` 按创建时间正序。

### ProjectSummary

```json
{
  "id": "…",
  "name": "我的婚纱照",
  "created_at": "…",
  "updated_at": "…",
  "stage": "style",
  "cover_asset_id": "…" | null,
  "photo_count": 0,
  "active_task_count": 2,
  "has_pending_recovery": false
}
```

- `cover_asset_id`：最早一张有版本的相册作品；否则最早一张有版本的样片；否则 null。
- `photo_count`：`kind = "photo"` 且有当前版本的作品数。
- `active_task_count`：`waiting + running` 任务数；已被手动重试任务替代的任务不计入。
- `has_pending_recovery`：存在 `held` 或 `interrupted` 任务；已被手动重试任务替代的任务不计入。

### ProjectDetail

ProjectSummary 加上：

```json
{
  "makeup_level": "natural",
  "person_a": Person,
  "person_b": Person,
  "previews": [Slot],
  "photo_sets": [PhotoSet],
  "photos": [Slot]
}
```

- `makeup_level`：新娘的妆容强度，`light`（淡妆）、`natural`（自然新娘妆，默认）或 `refined`（精致新娘妆），写进之后新建的样片和拍组的提示词；换动作、换景别沿用原提示词里的档位，加强长相一律用 `light`（docs/prompts.md 第 2.2 节）。
- `previews`：按内置风格顺序排列，只包含已创建的样片位置。
- `photo_sets`：按该风格第一次拍组的时间正序。
- `photos`：全部 `kind = "photo"` 的作品位置，按创建时间正序（同一批次内按动作分配顺序）。

### Batch

```json
{
  "id": "…",
  "project_id": "…",
  "batch_type": "preview",
  "style_id": null,
  "style_name": null,
  "created_at": "…",
  "status": "running",
  "total": 6,
  "counts": { "waiting": 0, "held": 0, "running": 2, "completed": 3, "failed": 1, "cancelled": 0, "interrupted": 0 }
}
```

`batch_type`：`preview | photo | regenerate`。`style_id` / `style_name`：拍组批次（`photo`）的风格；样片批次为 null。

批次的 `total`、`counts` 和 `status` 只汇总每条重试链最新的任务。若任务存在另一条任务的 `retry_of_task_id` 指向它，则该任务为 superseded（已被替代），不计入批次统计；`GET /api/tasks` 仍返回全部任务。

### Task

```json
{
  "id": "…",
  "project_id": "…",
  "batch_id": "…",
  "target_slot_id": "…",
  "status": "failed",
  "style_id": "korean_minimal",
  "pose_id": null,
  "moment_title": "海边踏浪回眸" | null,
  "moment_shot_size": "wide" | "full" | "three_quarter" | "half" | "close" | "medium" | "extreme_close" | "detail" | null,
  "prompt": "…",
  "error_code": "rejected",
  "error_message": "内容审核未通过，请调整照片后重试",
  "auto_retry_count": 0,
  "retry_of_task_id": null,
  "regenerate_mode": "same" | "identity_boost" | "change_pose" | "change_shot" | "face_swap" | null,
  "source_version_id": "…" | null,
  "model": "gpt-image-2",
  "created_at": "…",
  "started_at": "…",
  "finished_at": "…"
}
```

- `model`：任务创建时选定的模型（取自模型配置快照）；换脸任务为换脸模型名。
- `regenerate_mode`：任务的操作类型；首次生成为 null。
- `source_version_id`：换脸任务的输入版本；其他任务为 null。

### VersionDetail（历史版本，`GET /api/slots/{id}/versions`）

```json
{
  "id": "…",
  "asset_id": "…",
  "version_number": 2,
  "created_at": "…",
  "model": "gpt-image-2.5-flare" | null,
  "pose_id": "pose_03" | null,
  "pose_name": "新郎从背后拥抱新娘" | null,
  "regenerate_mode": "same" | "identity_boost" | "change_pose" | "change_shot" | "face_swap" | null,
  "source_version_id": "…" | null,
  "is_current": true
}
```

- `pose_id` / `pose_name`：生成该版本的任务所用动作（换动作后各版本可能不同）；样片为 null。
- `regenerate_mode`：生成该版本的任务属于哪种重生；首次生成为 null；换脸修正为 `face_swap`。
- `source_version_id`：同 VersionRef。换脸版本与其来源版本构图相同，前端据此默认用「切换」方式对比（PRD 4.6）。

### Moment（AI 导演写的一个镜头）

```json
{ "title": "海边踏浪回眸", "shot_size": "full", "orientation": "landscape", "bride_facing": "front", "groom_facing": "right", "scene": 1, "faces_clear": true, "text": "新娘提着裙摆踩着浪花向前跑，回头笑着看向镜头，新郎的脸朝画面右侧，牵着她的手跟在身后……" }
```

- `title`：2～12 个汉字，相册中显示的动作名称。
- `shot_size`：`wide`（远景）、`full`（全身）、`three_quarter`（七分身）、`half`（半身）、`close`（近景）。尚未拍摄的旧方案中的停用景别读取时改为最接近的一档：`medium`→`three_quarter`，`extreme_close`、`detail`→`close`。
- `orientation`：`portrait`（竖版）或 `landscape`（横版），由导演决定、用户可在方案预览中切换。具体比例见 `Settings.portrait_ratio`。在画幅功能之前保存的方案没有这个字段，读取时按 `portrait` 处理（当时所有照片都是竖版）。
- `bride_facing` / `groom_facing`：`front`、`left`、`right`，方向按图片所见。旧草稿缺少字段时读取为 `front`；新导演输出必须包含字段，并且非 `front` 的朝向要在 `text` 里写成「新娘的脸朝画面左侧」这样的原话（docs/prompts.md 第 7.2 节）。用户通过 PATCH 手动修改朝向时不做这项检查。
- `faces_clear`：两人面部是否清晰可见。背影、剪影、人物很小、被遮挡的氛围镜头为 `false`，这时提示词改用氛围画面质感段，作品不自动换脸（docs/prompts.md 第 2.5 节）。V3.35 以前的方案读取为 `true`；可以通过 PATCH 修改。
- `scene`：导演所选场景变体的编号（从 1 开始），成片提示词据此附上该场景的原文。V3.34 以前的方案为 `null`；不能通过 PATCH 修改，「换一个」会重新选择，改景别沿用原值。
- `text`：「镜头：」之后的正文，不含该前缀，以「。」结尾，最多 220 个字。

### MomentDraft（拍组前的导演方案）

```json
{
  "id": "…",
  "project_id": "…",
  "preview_slot_id": "…",
  "style_id": "seaside_sunset",
  "style_name": "海边日落",
  "director_model": "gpt-6.1-sol",
  "moments": [Moment],
  "used_by_batch_id": null,
  "created_at": "…",
  "updated_at": "…"
}
```

### FaceSwapStatus

```json
{ "available": true, "mode": "facefusion" | "mock" | null, "reason": null | "未配置 FaceFusion 安装目录" }
```

`available = false` 时 `reason` 为给用户看的中文原因，换脸接口返回 409 `face_swap_unavailable`（附同一原因）。

### Style

```json
{ "id": "korean_minimal", "name": "韩系极简", "description": "白纱、黑西装、简约影棚、柔光", "default_preview": true, "retired": false }
```

`default_preview`：生成样片时用户没有选择风格，就生成这些风格（内置顺序中未停用的前 6 种）。

`retired`：已停用的风格（V3.20 起：港风复古、城市夜景、冬日雪景、黑白经典、生活日常；V3.22 起：欧式教堂）。接口仍然返回它们，因为旧项目的样片和作品要显示风格名；前端选择风格时不列出，样片列表只在项目已有该风格样片时显示。生成样片时传入已停用的风格返回 422 `invalid_styles`，除非这个项目已经有该风格的样片（重新生成旧样片仍然允许）。旧作品的重新生成、换动作、换景别、加强长相不受影响。

### Model

```json
{ "id": "gpt-image-2.5-flare", "name": "GPT Image 2.5 Flare", "family": "openai", "max_reference_images": 10, "description": "OpenAI，按张计费，参考图≤10" }
```

首版内置模型（固定顺序）：

| id | family | max_reference_images |
|---|---|---|
| `gpt-image-2` | openai | 4 |
| `gpt-image-2.5-flare` | openai | 10 |
| `gpt-image-2.5-sunburst` | openai | 10 |
| `gemini-nano-banana-2.1` | gemini | 6 |

Seedream 暂不支持：它的本地参考图每次只能传 1 张，而每个任务至少需要 2 张（新娘、新郎）。

### Settings

```json
{ "default_model": "gpt-image-2", "batch_reference_mode": "prompt_only", "director_model": "gpt-6.1-sol" | null }
```

`batch_reference_mode`（PRD 3.3）：

| 值 | 拍组任务的参考图 |
|---|---|
| `prompt_only`（默认） | `[新娘, 新郎]`，风格只靠提示词描述 |
| `with_preview` | `[新娘, 新郎, 该样片当前版本]`，提示词注明只参考第三张的风格、不参考其人脸和构图 |

默认 `prompt_only`：样片里的人脸本身是模型的近似结果，作为参考可能让成片偏离本人长相。是否改为 `with_preview` 以实际对比为准。

`auto_face_swap`：生成后是否自动换脸，默认 `true`。FaceFusion 不可用时这个设置不生效。见下文「换脸修正」。

`portrait_ratio`：竖版比例，`3:4`（默认）、`2:3` 或 `9:16`；横版使用同一比例横过来（`4:3`、`3:2`、`16:9`）。样片固定为竖版；拍组按每个镜头的 `orientation` 选择。首次启动时取 `config.local.json` 的 `aspect_ratio`（是这三个值之一时），之后以设置为准。只影响之后新建的任务；重新生成、加强长相、换景别沿用原任务快照中的比例。

`director_model`：AI 导演使用的 88API 文字模型名称（任意 `/v1/chat/completions` 可用的模型，由用户填写）。首次启动时取 `config.local.json` 的 `director_model`，没有则为 null。为 null 时拍组和换动作不可用（409 `director_not_configured`）。模拟生图模式下使用模拟导演，不需要配置。

---

## 2. 接口

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/health` | — | `{"ok": true, "generator": "mock" \| "88api", "model": "<当前默认模型>", "face_swap": FaceSwapStatus}` |
| GET | `/api/models` | — | `Model[]` |
| GET | `/api/settings` | — | `Settings` |
| PATCH | `/api/settings` | `{"default_model"?: string, "batch_reference_mode"?: string, "director_model"?: string \| null, "portrait_ratio"?: string, "auto_face_swap"?: boolean}` | `Settings`；模型不在列表中时 422 `invalid_model`，参考方式无效时 422 `invalid_reference_mode`，竖版比例无效时 422 `invalid_portrait_ratio`，`director_model` 为空字符串时 422 `invalid_director_model` |
| GET | `/api/styles` | — | `Style[]`（内置风格，固定顺序，见 docs/prompts.md 第 3 节） |
| GET | `/api/projects` | — | `ProjectSummary[]`，按 `updated_at` 倒序 |
| POST | `/api/projects` | `{"name"?: string}` | `ProjectDetail`（201），默认名「我的婚纱照」 |
| GET | `/api/projects/{id}` | — | `ProjectDetail` |
| PATCH | `/api/projects/{id}` | `{"name"?: string, "person_a_role"?: Role, "makeup_level"?: "light" \| "natural" \| "refined"}` | `ProjectDetail`；字段为 null 或档位无效时 422 |
| DELETE | `/api/projects/{id}` | — | 204；存在 `running` 任务时 409 `project_busy`（先取消剩余任务） |
| POST | `/api/projects/{id}/images` | multipart：`person` = `a`/`b`，`angle` = `front`/`left`/`right`（默认 `front`），`file` | `ProjectDetail` |
| DELETE | `/api/projects/{id}/images/{person}/{angle}` | `person` = `a`/`b`，`angle` = `left`/`right` | 204；只清空侧照引用，保留图片资产 |
| GET | `/api/assets/{id}` | — | 图片文件 |
| POST | `/api/projects/{id}/previews` | `{"client_request_id": string, "style_ids"?: string[], "model"?: string}` | `Batch`（201）；`model` 缺省用默认模型，不在列表中时 422 `invalid_model` |
| POST | `/api/projects/{id}/moment-drafts` | `{"preview_slot_id": string, "count": 4 \| 8 \| 12 \| 20}` | `MomentDraft`（201）；同步调用 AI 导演 |
| GET | `/api/moment-drafts/{id}` | — | `MomentDraft` |
| PATCH | `/api/moment-drafts/{id}/moments/{index}` | `{"title"?: string, "shot_size"?: string, "orientation"?: string, "bride_facing"?: string, "groom_facing"?: string, "text"?: string, "faces_clear"?: boolean}` | `MomentDraft`；用户手动修改第 `index`（从 0 开始）个瞬间 |
| POST | `/api/moment-drafts/{id}/moments/{index}/rewrite` | `{"shot_size"?: string}` | `MomentDraft`；请 AI 导演只重写这一个。不传 `shot_size`：换一个新瞬间；传入：保持该瞬间的动作和画幅，改写为指定景别 |
| POST | `/api/projects/{id}/photo-batches` | `{"client_request_id": string, "draft_id": string, "model"?: string}` | `Batch`（201，`batch_type = "photo"`），张数 = 方案中瞬间的数量 |
| GET | `/api/projects/{id}/batches` | — | `Batch[]`，按创建时间倒序 |
| GET | `/api/batches/{id}` | — | `Batch` |
| POST | `/api/batches/{id}/continue` | — | `Batch`（`held` → `waiting`） |
| POST | `/api/batches/{id}/cancel` | — | `Batch`（`waiting`/`held` → `cancelled`） |
| POST | `/api/slots/{id}/regenerate` | `{"client_request_id": string, "mode": "same" \| "identity_boost", "model"?: string}` | `Batch`（201，`batch_type = "regenerate"`） |
| POST | `/api/slots/{id}/change-pose` | `{"client_request_id": string, "model"?: string}` | `Batch`（201）；仅 `kind = "photo"` |
| POST | `/api/slots/{id}/change-shot` | `{"client_request_id": string, "shot_size": "wide" \| "full" \| "three_quarter" \| "half" \| "close", "model"?: string}` | `Batch`（201）；仅 `kind = "photo"`；停用景别 `medium`、`extreme_close`、`detail` 不能作为目标（422） |
| POST | `/api/slots/batch-action` | `{"client_request_id": string, "slot_ids": string[], "action": "same" \| "identity_boost" \| "change_pose" \| "change_shot" \| "face_swap", "shot_size"?: string, "model"?: string}` | `Batch`（201），一个批次内每个位置一个任务；`action = "change_shot"` 时必须提供 `shot_size`；`action = "face_swap"` 时忽略 `model` |
| POST | `/api/slots/{id}/face-swap` | `{"client_request_id": string}` | `Batch`（201，`batch_type = "regenerate"`）；样片与相册作品均可 |
| GET | `/api/slots/{id}/versions` | — | `VersionDetail[]`，按 `version_number` 倒序 |
| POST | `/api/slots/{id}/restore` | `{"version_id": string}` | `Slot` |
| DELETE | `/api/slots/{id}` | — | 204；仅 `kind = "photo"`；存在 `running` 任务时 409 `slot_busy` |
| POST | `/api/projects/{id}/export` | `{"slot_ids"?: string[]}` | `application/zip`（附件下载），缺省为全部有版本的作品 |
| GET | `/api/tasks` | 查询参数 `project_id`、`batch_id`、`status`（均可选） | `Task[]`，按创建时间倒序 |
| POST | `/api/tasks/{id}/retry` | — | 新 `Task`（201）；仅 `failed`/`interrupted` 可重试 |
| POST | `/api/tasks/{id}/cancel` | — | `Task`；仅 `waiting`/`held` 可取消 |

### 规则补充

- **健康检查**：返回 `ok`、`generator`、`model`（当前默认模型）和 `face_swap`（FaceSwapStatus）；不调用模型服务，不验证账户余额。始终不返回 API Key。
- **默认模型**：保存在 SQLite（PRD 11.8 Settings），修改后立即对新任务生效，无需重启。首次启动时取 `config.local.json` 的 `model`（须在模型列表中），否则为 `gpt-image-2`。
- **模型配置快照**：新建任务的 `model_config_snapshot` 记录 `generator`、`model`、`base_url`、`aspect_ratio`、`resolution`，绝不记录 API Key。调度器按快照里的 `model` 和 `aspect_ratio` 调用模型（`aspect_ratio` 是这个任务自己的比例，见 `Settings.portrait_ratio`），所以修改默认模型不影响已创建的任务；手动重试沿用原任务的快照（同一模型）。模拟生成器同样记录所选模型名，但不调用任何模型。
- **「结果无法确认」的提示文字按原因区分**（都标为 `interrupted`，都不自动重发）：
  - 后端重启时请求仍在进行（启动恢复）：「后端重启时这次请求还在进行，结果无法确认，可能已经扣费。重新生成可能产生额外费用」，`error_code = "interrupted_by_restart"`。
  - 请求发出后连接中断、读写超时或协议错误：「模型服务连接中断，结果无法确认，可能已经扣费。重新生成可能产生额外费用」，`error_code = "unknown_outcome"`。
  - 生成成功但图片下载失败：「图片已生成但下载失败，可能已经扣费。重新生成可能产生额外费用」，`error_code = "download_failed"`。
  - 502/504：保持现有文字，`error_code = "unknown_outcome"`。
- **真实生成错误**：未建立连接为 `not_sent`，可自动重试；请求发送后读取、写入、协议异常以及生成成功后的图片下载失败为 `unknown_outcome`，任务保持 `interrupted`，不能自动重发。鉴权或余额不足为 `auth_or_quota`，暂停同批等待任务；HTTP 429 为 `rate_limited`，按 `Retry-After` 全局退避；审核/参数拒绝、服务端 HTTP 5xx、成功响应没有图片为 `rejected`。用户只看到中文提示，服务商原始错误只写入脱敏日志。真实调用与用户主动重试均可能收费。
- **上传照片**：替换照片不删除、不清除任何已有内容；用旧照片生成的样片和作品通过 `is_stale` 标出（PRD 15.3）。旧图片文件不删除，因为可能仍被任务引用。
- **生成样片**：两张照片齐全才允许，否则 409 `photos_missing`。`style_ids` 缺省为默认风格（`Style.default_preview` 为 true 的前 6 种）；前端在用户未选风格时不传该字段，「重新生成全部」只传已有样片的风格。每种风格复用该项目已有的样片位置（没有则新建），所以重生样片会产生新版本。
- **防重复提交**：同一 `client_request_id` 再次提交，返回已存在的批次（200），不新建任务。
- **手动重试**：任务只能成功手动重试一次。原任务行永不修改，新任务通过 `retry_of_task_id` 指向原任务；再次重试已被替代的任务返回 409 `task_already_retried`（`该任务已经重试过，请查看最新的任务`）。批次统计、项目任务统计和 `Slot.latest_task` 均只考虑重试链中最新的任务。
- **AI 导演（moment-drafts）**（PRD 3.3）：
  - 照片须齐全（409 `photos_missing`），样片须属于本项目且有当前版本（404 `slot_not_found` / 409 `slot_not_ready`），`count` 不在 4/8/12/20 中时 422 `invalid_count`，未配置导演模型时 409 `director_not_configured`（「请先在设置中填写 AI 导演使用的文字模型」）。
  - 发给文字模型的只有文字：风格名、该风格的「服装与场景光线」文字、张数、规则，以及本项目该风格已有作品的瞬间标题（用于避开重复）。**不发送任何照片。**导演提示词见 docs/prompts.md 第 7 节。
  - 后端校验（每个瞬间与整组）：字段齐全且合法；`title` 2～12 个汉字、组内不重复且不与避开列表重复；`text` ≤ 220 个字、以「。」结尾、不含 docs/prompts.md 第 7.3 节的禁用词；`shot_size` 是四种之一。**不校验整组的景别数量分布**，景别由导演按场景自由选择。不通过时把具体问题告诉文字模型并自动重写一次；仍不通过返回 502 `director_invalid_output`（「AI 导演没有写出符合要求的方案，请重试」）。
  - 文字模型调用失败（网络、鉴权、余额、超时）返回 502 `director_failed`，附中文原因（鉴权/余额与图片模型使用相同提示）；不产生任何生成任务。
  - 用户修改（PATCH）同样执行单个瞬间的校验，不通过时 422 `invalid_moment` 并说明原因。
  - 方案被用于拍组后（`used_by_batch_id` 非空），PATCH / rewrite / 再次拍组均返回 409 `draft_used`。
- **拍一组（photo-batches）**：
  - 使用 `draft_id` 指定的导演方案，按方案顺序为每个瞬间创建一个作品位置和任务；任务记录 `moment_title`、`moment_shot_size`，`pose_id` 为 null；镜头段为「镜头：」+ `text`。创建成功后将方案标记为已使用。
  - `draft_id` 必填；草稿须属于本项目且尚未使用，否则返回 404 `draft_not_found` 或 409 `draft_used`。草稿关联的样片须有当前版本，否则 409 `slot_not_ready`。照片须齐全，否则 409 `photos_missing`。
  - 为每张新建一个 `kind = "photo"` 的作品位置（`style_id` 为该样片的风格）和一个任务（`style_id` 已填，`pose_id` 为 null）。
  - 参考图按 `batch_reference_mode` 决定（见 Settings）；参考图数量超过所选模型的 `max_reference_images` 时 422 `invalid_model`。
  - 提示词按 `docs/prompts.md` 组合：画幅 + 人物锁定 + 服装与场景 + 导演写出的镜头 + 画面质感 + 避免项。
  - 批次记录所用样片版本（模型配置快照之外，另存 `reference_preview_version_id`），即使之后样片被重生，也能追溯这一组参考的是哪个版本。
  - 防重复提交规则与样片相同。
- **参考图顺序**：新娘的照片永远排第一张。
- **重生（regenerate）**（PRD 4.1）：
  - 该位置必须有至少一个任务（有生成上下文），否则 409 `slot_not_ready`；该位置已有 `waiting`/`running`/`held` 任务时 409 `slot_busy`（避免同一张重复排队花钱）。
  - `mode = "same"`：复制该位置**当前版本**对应任务的参考图、风格、动作和提示词（没有当前版本时用最新任务），只按需替换模型。即使人物照片已更换，也沿用原参考图。
  - `mode = "identity_boost"`（加强长相重试）：使用项目**当前**的人物照片，按 docs/prompts.md 第 6 节重新组合提示词；`with_preview` 设置照常生效。
  - 新任务记录 `regenerate_mode`（SQLite `generation_tasks.regenerate_mode` 允许 `NULL`、`same`、`identity_boost`、`change_pose`、`change_shot`、`face_swap`）；首次生成和既有任务的手动重试默认保留原有操作类型。成功后新增版本并设为当前版本；失败不影响当前版本。
  - `model` 缺省时，`same` 沿用源任务模型配置快照，其他模式使用当前默认模型；显式传入时只替换快照中的模型 ID。参考图数量必须符合该模型的 `max_reference_images`。
- **换动作（change-pose）**（PRD 4.2）：同步请 AI 导演为这张照片写 1 个新瞬间（避开该风格所有已有瞬间标题，景别不限），校验规则同导演方案；然后用项目当前人物照片与该位置的风格创建任务。未配置导演模型时 409 `director_not_configured`，导演失败时 502 `director_failed` / `director_invalid_output`，均不创建任务。成功后该位置的 `pose_name` / `shot_size` 显示为当前版本的瞬间。
- **换景别（change-shot）**（PRD 4.2.1）：
  - 旧作品中的局部特写（`detail`）没有人脸：对它加强长相或换脸返回 409 `no_faces`；重新生成后也不会自动换脸。
  - 同步请 AI 导演把该位置当前版本对应任务的瞬间改写为目标景别（docs/prompts.md 第 7.4 节），校验规则同导演方案，且返回的 `shot_size` 必须等于目标景别；然后用项目当前人物照片与该位置的风格创建任务，`regenerate_mode = "change_shot"`。
  - 目标景别与当前相同时 422 `same_shot_size`；旧版固定动作的作品（无 `moment_title`）同样可用：以该动作文字作为改写起点。
  - 不把已生成的图片作为参考图。导演失败、未配置导演模型等错误同换动作，均不创建任务。
- **多选操作（batch-action）**：`slot_ids` 须全部属于同一项目、无重复，否则 422 `invalid_slots`；任一位置不满足单张操作的条件时整批 409（错误信息指出是哪一张），不部分创建。`change_pose` 时一次请 AI 导演写出所需数量的新瞬间（同批互不重复），任一失败则整批不创建。
- **历史版本与恢复**（PRD 4.4）：恢复只修改 `current_version_id`，不复制、不删除文件。`version_id` 必须属于该位置，否则 404 `version_not_found`。
- **删除作品**：删除位置、其全部版本记录与这些版本的图片文件；先取消该位置的 `waiting`/`held` 任务。任务记录保留（供追溯），`output_asset_id` 清空；已删除位置的 `target_slot_id` 是历史标识，不再要求对应一个现存位置。
- **导出 ZIP**：只打包当前版本；文件名为 `{两位序号}_{风格名}_{动作名}.{扩展名}`，序号按相册顺序，并清理文件名中的路径分隔符；附件名同时提供 ASCII 回退名和 RFC 5987 UTF-8 名（`vowframe-<项目名>.zip`）。`slot_ids` 中有不属于本项目或没有当前版本的位置时 422 `invalid_slots`。
- **换脸修正（face-swap）**（PRD 4.5）：
  - 配置：`config.local.json` 的 `facefusion_dir`（FaceFusion 安装目录，须包含 `run.sh` 与 `.venv`）。环境变量 `VOWFRAME_FACE_SWAPPER=mock` 强制使用模拟换脸（测试与隔离端到端验证用：复制输入图片，不调用 FaceFusion）。未配置且未设置模拟时不可用，**不回退到任何默认目录**。
  - 请求校验：不可用时 409 `face_swap_unavailable`；该位置没有当前版本时 409 `slot_not_ready`；有 `waiting`/`running`/`held` 任务时 409 `slot_busy`；照片缺失时 409 `photos_missing`。
  - 输入版本：当前版本；若当前版本本身是换脸结果，改用它的 `source_version_id` 指向的版本（来源版本已不存在时 409 `slot_not_ready`）。任务记录 `source_version_id`。
  - 任务字段：`regenerate_mode = "face_swap"`；`reference_image_ids = [新娘正面, 新郎正面, 新娘左, 新娘右, 新郎左, 新郎右]`（项目当前照片，没有的侧面照为 null；自动换脸同样取项目当前照片；2026-10-28 之前的任务只有两张正面）；`style_id`、`pose_id`、`moment_title`、`moment_shot_size` 复制自输入版本的任务，所以成功后作品的动作名与景别不变；`prompt` 为空字符串；`model_config_snapshot = {"generator": "facefusion" | "mock", "model": "hyperswap_1c_256/hyperswap_1b_256", "front_model": "hyperswap_1c_256", "side_model": "hyperswap_1b_256", "side_turn": 0.18, "pixel_boost": "512x512", "face_mask_types": ["box", "occlusion"], "weight": 0.5 | 1.0}`；换脸成功后追加 `faces`（每人的 `turn`、`facing`、`model`、`sources`，侧面照里检测不到对应性别的脸时另有 `side_photo_unusable: true`），`model` 改为实际的「新娘/新郎」模型（相册作品 0.5，样片 1.0）。其中 `model` 是界面显示的版本模型摘要。
  - 执行（FaceFusion）：一个进程内完成检测与两次换脸。先检测新娘、新郎原照的性别，不符时失败 `roles_swapped`（「新娘和新郎的照片可能放反了，请在工作台点『交换』后重试」）；再检测输入图片，缺少女性脸或男性脸时失败 `face_not_found`（「没有识别到新娘的脸，可能是侧脸角度过大或被遮挡」，新郎同理，两者都缺时合并说明）。然后按每人在输入图片里的转头程度选模型和源照片（规则见 PRD 4.5：正脸 `hyperswap_1c_256` + 正面照，侧脸 `hyperswap_1b_256` + 正面照与对应侧面照），先换女性脸、再换男性脸。实际用到的模型均须在 profile 中确认执行过 `CUDAExecutionProvider` 运算，否则任务失败。模型是用户在 2026-10-27 对真实照片比较后选定的；`512x512` 像素提升当时直接采用，2026-10-28 与 `256x256` 对比后保留；遮罩 `box` + `occlusion` 同日选定；相册强度同日改为 `0.5`，样片强度 2026-10-28 定为 `1.0`（见 PRD 4.5）。两个强度定义在后端常量 `FACE_SWAP_WEIGHT`、`PREVIEW_FACE_SWAP_WEIGHT`，创建任务时按目标图片类型写入快照，执行时按快照传给 FaceFusion。
  - 失败处理：`roles_swapped`、`face_not_found`、`reference_missing` 直接失败，不自动重试；FaceFusion 进程崩溃、超时（120 秒）或没有输出文件为 `face_swap_failed`（「换脸处理失败，请稍后重试」），自动重试最多 2 次。
  - 调度：换脸任务单独一条队列，同时最多运行 1 个；不计入生图并发 `max_concurrency`，不受 429 退避影响，鉴权/余额搁置也不影响它。
  - 启动恢复：`running` 的换脸任务改回 `waiting`（本地、不收费，可安全重做），`waiting` 的换脸任务保持 `waiting`，不改为 `held`。
  - 成功后新增版本并设为当前版本；失败不影响当前版本。`is_stale` 照常计算。
  - **自动换脸**：设置 `auto_face_swap = true`（默认）且 FaceFusion 可用时，任何非换脸任务成功保存版本的**同一个事务**里，再插入一个换脸任务：同一批次、同一位置，`source_version_id` 为刚保存的版本，`reference_image_ids` 为该生图任务的前两张参考图（生成时用的新娘、新郎照片），其余字段同上。换脸任务本身完成后不会再触发换脸。
  - **对换脸版本的其他操作**：重新生成、加强长相、换动作、换景别以及导出时读取的「来源任务」，会沿 `source_version_id` 追溯到生成该图的生图任务，而不是换脸任务（换脸任务的 `prompt` 为空）。
- **数据库升级**：启动时通过 `PRAGMA table_info` 检查 `generation_tasks.regenerate_mode`。升级保留现有任务和版本数据；为使删除作品后仍能保留任务行，任务表升级时会以事务复制原有列、增加可空的 `regenerate_mode`，并解除任务目标位置的级联外键；删除后 `target_slot_id` 继续保存原位置 ID 供追溯。
