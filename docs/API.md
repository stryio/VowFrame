# VowFrame 后端 API 约定（骨架阶段）

前后端共同遵守的接口契约。实现与本文不一致时，以修改本文为先。

- 基础路径：`/api`
- 数据格式：JSON（上传为 `multipart/form-data`）
- 时间：ISO 8601 字符串（UTC）
- ID：字符串（UUID4 hex）
- 错误：HTTP 4xx/5xx，响应体 `{"detail": {"code": "<snake_case>", "message": "<给用户看的中文>"}}`

本阶段范围：项目、上传、风格样片、按风格拍组（批量生成）、相册（单张/多选重生、加强长相重试、换动作、换景别、AI 修脸、换脸修正、历史版本与恢复、删除、ZIP 下载）、任务与批次控制。

**一个项目可以拍多组风格**：试拍的每张样片都可以「确认样片，拍一组」，次数不限；不存在「项目唯一选中风格」。

默认流程先选择服饰和妆发，在首张样片中一起完成穿衣与摄影效果，再确认样片并拍组照。
更换当前服饰或妆发只影响之后新建的样片，沿用旧样片拍组仍使用该样片创建时的选择快照。

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
{ "asset_id": "…" | null, "left_asset_id": "…" | null, "right_asset_id": "…" | null, "left_full_asset_id": "…" | null, "right_full_asset_id": "…" | null, "role": "bride" }
```

`left_asset_id` / `right_asset_id` 是可选的侧面参考照；方向均按图片所见（`left` = 脸朝画面左侧）。V7.48 起它们是半侧脸（约 45°），`left_full_asset_id` / `right_full_asset_id` 是正侧面（约 90°）；每个方向最多两张。AI 导演设计面部清晰的镜头时，每个人只能用有照片的角度（docs/prompts.md 第 7.2 节）。

### VersionRef

```json
{ "id": "…", "asset_id": "…", "version_number": 1, "created_at": "…", "model": "gpt-image-2" | null, "source_version_id": "…" | null,
  "regenerate_mode": "hand_fix" | null, "face_scores": { "bride": 0.62, "groom": null } | null }
```

- `model`：生成该版本的任务所用模型（取自任务的模型配置快照）；换脸版本为实际处理人物所用模型，两人都换时为「新娘/新郎」，如 `hyperswap_1c_256/hyperswap_1b_256`；V7.13 只换新郎时只记录新郎的模型，如 `hyperswap_1b_256`（2026-10-28 之前的换脸版本为 `hyperswap_1a_256/hyperswap_1c_256`）；模拟生成或无法确定时为 null。既有版本仍按其创建时的快照显示。
- `source_version_id`：换脸、AI 修脸和修手版本的来源版本（修正时输入的那个版本）；其他版本为 null。
- `regenerate_mode`（V7.25）：生成该版本的任务类型，同 VersionDetail；界面据此把修正标成「修脸 · 对比」或「修手 · 对比」。
- `size_warning`：服务商返回的尺寸和请求不同时的说明，例如 `要求 1536×2048，实际 1086×1448`；相同或不比较（Gemini、模拟生成）时为 null。换脸版本沿用来源版本的值。
- `face_scores`：新娘、新郎各自与生成该版本所用正脸照（V7.33 起为正面造型照；V4.0 以前的作品为上传照片）的人脸相似度（FaceFusion ArcFace 余弦相似度，保留 3 位小数，PRD 4.6）。某人为 null 表示画面里没找到这个人的脸；整个字段为 null 表示尚未测量（FaceFusion 不可用、细节特写、或排队中）。
  - 测量时机：换脸任务在同一次 FaceFusion 调用里测量换脸前后两张图，换脸前的分数写到来源版本（来源版本已有分数时保留原值）；没有排队换脸的生成版本，在换脸通道空闲时单独测量（每次最多 20 张）。后端启动时为所有尚未测量的版本补算。测量失败只记日志，不重试，下次启动再试。

### TaskBrief

```json
{ "id": "…", "status": "running", "error_code": null, "error_message": null, "model": "gpt-image-2.5-flare" | null, "regenerate_mode": "face_swap" | null }
```

`regenerate_mode`：同 Task.regenerate_mode。界面据此区分「换脸修正中 / 换脸失败」「AI 修脸中 / 修脸失败」与「重新生成中 / 生成失败」，因为两者的提示和费用含义不同。

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
  "is_stale": false,
  "styled_asset_ids": {"bride": "…", "groom": "…"} | null,
  "look_ids": {"bride": "…", "groom": "…"} | null
}
```

- `latest_task`：以该位置为目标、创建时间最新且未被其他任务替代的任务。
- `pose_id`：旧版固定动作的编号；AI 导演写的瞬间为 null。
- `pose_name`：相册中显示的动作名称。AI 导演写的瞬间为其 `title`，旧版固定动作为动作名；样片为 null。均取自生成当前版本的任务（无版本时取最新任务）。
- `shot_size`：`"wide" | "full" | "three_quarter" | "half" | "close" | "medium" | "extreme_close" | "detail" | null`，同样取自对应任务；旧版固定动作与样片为 null。`wide`（远景）自 V3.14 起重新启用；`medium`（中景）、`extreme_close`（特写）、`detail`（局部特写）是 V3.4～V3.11 的停用景别，只会出现在旧作品上。
- `batch_id`：创建该位置的批次。
- `is_stale`（由后端计算）：当前版本是用旧照片生成的，即生成它的任务的正面照或所用侧面照已不再是项目当前对应照片。造型照按它来自的上传照片判断（V4.0）。样片参考图不参与侧照 stale 判断。没有当前版本时为 false。
- `styled_asset_ids`（V4.0）：当前版本所用的两人正面造型照（图片用 `GET /api/assets/{id}` 读取）；V4.0 以前的作品和没有当前版本时为 null。
- `look_ids`（V5.0）：当前版本所用的两人造型（`Look.id`）；当前版本是换脸结果时，取换脸前那张图的造型。V5.0 以前的作品和没有当前版本时为 null。

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
  "has_pending_face_scores": false,
  "makeup_level": "natural",
  "groom_makeup_level": "natural",
  "bride_retouch_settings": RetouchSettings,
  "groom_retouch_settings": RetouchSettings,
  "bride_hair_length": "long",
  "groom_hair_length": "short",
  "person_a": Person,
  "person_b": Person,
  "previews": [Slot],
  "photo_sets": [PhotoSet],
  "photos": [Slot],
  "looks": [Look],
  "style_looks": {"korean_minimal": {"bride": "…" | null, "groom": "…" | null}, …},
  "style_outfits": {"korean_minimal": "satin_a_line", …},
  "side_looks": [SideLook],
  "makeups": [Makeup],
  "default_look_items": {"bride": [LookItem], "groom": [LookItem]}
}
```

- `has_pending_face_scores`：本项目是否还有排队中或计算中的后台人脸相似度评分，涵盖样片、成片、妆容及正侧面造型。图片生成完成后，评分可能仍在进行，前端此时继续轮询项目详情；评分完成、未识别人脸、失败或评分服务不可用后停止等待。评分状态与分数在同一数据库快照中读取，不能用分数为 null 来判断是否还应轮询。
- `looks`（V5.0）：两人的正面造型候选（不含已删除的和侧面造型），按创建时间排列。
- `side_looks`（V7.24）：新照片会用到的侧面造型，按创建时间排列；V7.30 起也包括排队中、生成中和失败的（每个方向列最新一张，做好的优先）。每套没删除的正面造型、每个已上传的侧面方向最多一张，是从当前的侧面照做出来的；侧面照换过以后，旧侧面照做的不再列出。
- `style_looks`（V7.0，取代 `default_looks`）：每个风格生成新样片时使用的造型：新娘为这个风格选用的造型，新郎选用的造型（所有风格共用）；还没有选用时为 null。
- `style_outfits`（V7.17）：每个在用风格的新样片穿哪套固定服饰：项目选过的，否则是这个风格的第一套。已有样片和它的成片不随选择改变。
- `makeups`（V7.8）：新娘各个风格的妆容候选，以及（V7.22）新郎的修脸照候选（不含已删除的），按创建时间排列。每张带 `role`；新郎的 `style_id`、`makeup_color`、`style_name`、`makeup_style_id`、`makeup_style_name` 都是 `null`，所有风格共用。V7.21 起每张带 `style_name`、`makeup_style_id` 和 `makeup_style_name`：这张妆是按哪个风格的妆色和妆面画的；不是复用来的就等于它自己的风格。造型（`Look`）也带 `makeup_style_id`，新郎的为 `null`。
- `default_look_items`：按项目里两人的头发长度给出的建议发型。新郎默认勾选全部；新娘每个风格只勾第一个适合这个风格的（docs/prompts.md 第 8.1 节）。

### RetouchSettings（V7.75 独立精细调整；V7.76 扩为十一档）

```typescript
interface RetouchSettings {
  slimming: AdjustmentLevel;
  brightening: AdjustmentLevel;
  concealing: ConcealingLevel;
  chin: AdjustmentLevel;
  nasolabial: AdjustmentLevel;
}
```

- 分别表示下颌两侧收窄、肤色提亮、遮瑕、下巴下方软组织整理、法令纹淡化。各字段均必填，拒绝 `null`、未知字段、未知档位和数字百分比；它们表示生成方向，不承诺精确几何变形。
- `AdjustmentLevel` 从 0～10 依次为 `off`、`minimal`、`subtle`、`light`、`mild`、`balanced`、`moderate`、`enhanced`、`strong`、`intense`、`maximum`。`ConcealingLevel` 同样为十一档，第 5 档使用 `standard`，第 10 档使用 `full`，其余与通用调整一致。原值的含义不变。界面用滑杆展示，松手、键盘调整结束或离开控件时保存，拖动途中不提交请求。
- 项目的 `bride_retouch_settings` / `groom_retouch_settings` 保存两人的设置，新娘各风格共用。`PATCH /api/projects/{id}` 可提交某一人的完整设置，不影响妆感档位和另一人；仅修改姓名、头发等不重置设置。
- 提交 `makeup_level` / `groom_makeup_level` 表示选择快捷预设，会重置对应人的精细设置。`light` 默认 `{slimming: off, brightening: light, concealing: light, chin: off, nasolabial: off}`；`natural` 默认 `{slimming: off, brightening: moderate, concealing: standard, chin: light, nasolabial: light}`；`refined` 默认 `{slimming: light, brightening: moderate, concealing: full, chin: light, nasolabial: light}`。同一请求同时提供完整设置时，以显式设置为准。
- 创建妆容时在同一事务内读取项目设置，并把完整设置存入 `Makeup.retouch_settings`；之后更改项目、重复提交同一 `client_request_id`、失败重试或跨风格复用，都不会改写原候选的设置与提示词。发型记录 `Look.retouch_settings` 和 `makeup_level` 从其选中的妆容继承，之后的侧脸沿用该记录，与项目当前设置无关。
- 旧数据库自动补列；旧候选按自己记录的妆容档位返回对应设置，其图片、提示词与可用状态保持不变，不因新增控制而要求重新生成。

### Look（V5.0 造型候选；V7.0 起新娘按风格做，带妆）

```json
{
  "id": "…",
  "person": "a" | "b",
  "role": "bride" | "groom",
  "style_id": "korean_minimal" | null,
  "hairstyle_id": "low_bun",
  "hairstyle_name": "低盘发",
  "makeup_color": "classic_red" | null,
  "makeup_color_name": "正红朱砂" | null,
  "makeup_level": "light" | "natural" | "refined",
  "hair_length": "long",
  "retouch_settings": RetouchSettings,
  "status": "waiting" | "running" | "ready" | "failed" | "interrupted",
  "error_code": string | null,
  "error_message": string | null,
  "asset_id": "…" | null,
  "source_asset_id": "…",
  "selected": true,
  "selected_order": 1 | null,
  "usable": true,
  "unusable_reason": string | null,
  "face_score": 0.62 | null,
  "created_at": "…",
  "makeup_asset_id": "…" | null,
  "makeup_face_score": 0.64 | null,
  "selected_by": "user" | "auto" | null,
  "makeup_id": "…" | null
}
```

- `style_id`（V7.0）：新娘造型是为哪个风格做的，妆色和妆面按这个风格；新郎的造型所有风格共用，为 null；V7.0 以前的造型也为 null。
- `role`、`makeup_level`、`hair_length`：生成时这个人的角色、档位和头发长度。V7.0 起造型一次修图做好发型和妆容，编辑结果原样保存（不贴回原脸，docs/prompts.md 第 8 节）；档位决定妆容和精修的强度以及头发的完成度。`makeup_color` 是新娘造型的色系（这个风格的主推色系），新郎为 null；V6.x 的造型也为 null。
- `status` 为 `failed` 时，`error_code` 是生成失败的原因（与镜头任务相同）。V6.x 的 `look_no_face`、`look_face_lost`、`mask_editor_missing`、`face_model_missing`、`face_model_runtime_missing` 不再出现，旧记录里可能还留着。
- `selected` / `selected_order`（V7.0）：每个风格只有一套选用的新娘造型，新郎只有一套；选用的为 1，其余为 null。`selected_by`（V7.5）：`user` 是用户点了「选用」，`auto` 是系统选的（有相似度分数后选最像的一套，没有分数时选第一套生成好的）；用户选过的不再自动更换。
- `makeup_asset_id` / `makeup_face_score`（V7.5）：新娘正面造型分两次编辑，这是第一次化好妆、还没改发型的照片和它与上传照的相似度；新郎和侧面造型为 null。发型这一步失败后重试，直接从这张照片开始。`makeup_id`（V7.8）：新娘造型是在哪张妆容候选上改的发型，这时 `makeup_asset_id` 就是那张妆容的图片，`makeup_face_score` 是它的分数；V7.8 以前的新娘造型、新郎和侧面造型为 null。

### SideLook（V7.24 列出侧面造型）

```json
{"id": "…", "parent_look_id": "…", "person": "b", "role": "groom", "angle": "left",
 "status": "ready", "error_message": null, "asset_id": "…" | null, "source_asset_id": "…", "created_at": "…"}
```

侧面造型是把上传的侧面照修成和正面造型（`parent_look_id`）同一套妆发（docs/prompts.md 第 8 节）；V7.46 起只发侧面照，妆发按正面造型的发型和妆容档位用文字描述，不再发正面造型照。V7.30 起在造型这一步由用户点「做侧面造型」生成（`POST /api/looks/{id}/side-looks`），`status` 与造型候选相同；拍组时镜头需要而还没有做好的，仍在任务执行时补做一次。同一张正面造型图、同一个方向都复用做好的那张：V7.31 起别的风格复用的造型（同一张图的复制行）也共用它，`parent_look_id` 列的是各自那一行。`source_asset_id` 是做它用的侧面照；`facing`（V7.48）是这张侧面照所在的位置：`left`/`right` 半侧，`left_full`/`right_full` 正侧面（`angle` 仍只写方向）。`face_score`（V7.45）：和这张侧面照的相似度，做好后在后台打分，还没打分、没检测到人脸或换脸程序不可用时为 null；只用来显示，不会自动重做，也不影响正面造型的选用。

### OutfitLook（V7.18，V7.19 已移除）

服饰定妆照的接口（`/projects/{id}/outfit-looks`、`/outfit-looks/{id}/…`）和项目详情里的 `outfit_looks` 字段已移除。服饰只按所选服饰的文字在样片中完成（docs/prompts.md V7.19）。

### Makeup（V7.8 新娘妆容候选）

```json
{
  "id": "…",
  "person": "a" | "b",
  "style_id": "korean_minimal",
  "makeup_color": "milk_tea",
  "makeup_color_name": "奶茶裸棕" | null,
  "makeup_level": "light" | "natural" | "refined",
  "status": "waiting" | "running" | "ready" | "failed" | "interrupted",
  "retouch_settings": RetouchSettings,
  "error_code": string | null,
  "error_message": string | null,
  "asset_id": "…" | null,
  "source_asset_id": "…",
  "selected": true,
  "selected_by": "user" | "auto" | null,
  "usable": true,
  "unusable_reason": string | null,
  "face_score": 0.64 | null,
  "created_at": "…"
}
```

- 新娘在上传照上化好这个风格的妆、头发不动的样子（docs/prompts.md 第 8.2 节第一次编辑），每张一次付费编辑，图片类型 `styled`。
- `selected` / `selected_by`：每个风格只选用一张，用户选过的不再自动更换。V7.75 起按记录的 `retouch_settings.slimming` 判断：同组可用候选中有任何一张开启瘦脸，就不按分数在不同修整程度之间排名，保留当前选用项，尚未选用时先用第一张生成好的；全组关闭瘦脸时仍按相似度选。旧精致档按其原有轻微收窄规则处理。之后这个风格新做的新娘造型都在选中妆容上改发型。
- `usable`、`unusable_reason`、`face_score`：含义与 Look 相同。
- `usable`：能否用于新照片。换了这个人的正面照（`unusable_reason`「这个人的照片已经更换……」）、交换了新娘和新郎，或者是 V7.0 以前的造型（「这是旧版本的造型……」）时为 false；还没生成好时也为 false，但 `unusable_reason` 为 null。
- `face_score`：造型与上传照片的人脸相似度（FaceFusion 的 ArcFace 余弦相似度）；换脸不可用、还没测或没检测到脸时为 null。重试生成后重新计算。

`LookItem`：`{"hairstyle_id": string}`（妆色由风格决定，带上 `makeup_color` 会 422）。

- `makeup_level`：新娘的妆感及快捷预设，`light`（清透新娘妆）、`natural`（标准婚纱妆，默认）或 `refined`（精致影楼妆）。提亮、遮瑕与轮廓修整可通过 `bride_retouch_settings` 独立调整（docs/prompts.md 第 8.5.1 节），只影响之后新生成的妆容；发型继承选中的妆容，样片和照片照着造型的妆拍。已有的造型和照片不变。
- `groom_makeup_level`：新郎的精修强度，`light`（清爽）、`natural`（精修，默认）或 `refined`（精致，化淡淡的男士妆），规则与 `makeup_level` 相同。
- `bride_hair_length` / `groom_hair_length`：两人头发的实际长度，新娘 `long`（长发，默认）/ `medium`（及肩）/ `short`（短发），新郎 `buzz`（寸头）/ `short`（短发，默认）/ `medium`（中长发）。V5.0 起决定能生成哪些发型的候选（docs/prompts.md 第 8.2 节）；已有的造型不变。
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
  "counts": { "waiting": 0, "held": 0, "running": 2, "completed": 3, "failed": 1, "cancelled": 0, "interrupted": 0 },
  "paused": false
}
```

`paused`（V7.12）：用户暂停了这批任务，排队的任务都是 `held`，点继续前不会发出。

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
  "regenerate_mode": "same" | "identity_boost" | "change_pose" | "change_shot" | "face_swap" | "face_fix" | "hand_fix" | "wash" | "regional_edit" | null,
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
  "regenerate_mode": "same" | "identity_boost" | "change_pose" | "change_shot" | "face_swap" | "face_fix" | "hand_fix" | "wash" | "regional_edit" | null,
  "source_version_id": "…" | null,
  "is_current": true,
  "face_scores": { "bride": 0.62, "groom": 0.55 } | null
}
```

- `pose_id` / `pose_name`：生成该版本的任务所用动作（换动作后各版本可能不同）；样片为 null。
- `regenerate_mode`：生成该版本的任务属于哪种重生；首次生成为 null；换脸修正为 `face_swap`；AI 修脸为 `face_fix`（V7.1）；修手为 `hand_fix`（V7.25）；洗图为 `wash`（V7.26）。
- `source_version_id`：同 VersionRef。换脸版本与其来源版本构图相同，前端据此允许用「切换」方式对比（PRD 4.6）。
- `face_scores`：同 VersionRef。

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
{ "id": "gpt-image-2.5-flare", "name": "GPT Image 2.5 Flare", "family": "openai", "max_reference_images": 10, "description": "OpenAI，按张计费，参考图≤10", "providers": ["88api"] }
```

内置模型（固定顺序）；`providers`（V7.11）是提供它的生图服务：

| id | family | max_reference_images | providers |
|---|---|---|---|
| `gpt-image-2` | openai | 4 | `88api`、`pptoken` |
| `gpt-image-2.5` | openai | 10 | `pptoken` |
| `gpt-image-2.5-flare` | openai | 10 | `88api`、`pptoken` |
| `gpt-image-2.5-sunburst` | openai | 10 | `88api`、`pptoken` |
| `gpt-image-2.6` | openai | 10 | `88api`、`pptoken` |
| `gpt-image-2.6-sunburst` | openai | 10 | `88api`、`pptoken` |
| `gpt-image-2.6-flare` | openai | 10 | `88api`、`pptoken` |

GPT 系列的画质参数（2026-11-05）：发往 PPTOKEN 用 `max`，发往 88API 用 `xhigh`（88API 文档只给 az 系列列了更高档位）。
| `gemini-nano-banana-2.1` | gemini | 6 | `88api` |
| `gemini-3-pro-image`（Nano Banana Pro，V7.28） | gemini | 14 | `88api` |
| `doubao-seedream-5-5-261028`（Seedream 5.5，2026-11-13） | seedream | 10 | `ark` |

`ark`（火山方舟，2026-11-13）：官方 Seedream 接口 `POST {ark_base_url}/images/generations`，JSON 请求，参考图为
`data:` URI 数组，`size` 用和 GPT 系列同一张表的像素（只支持 1K/2K，4K 在发送前以 `not_sent` 拒绝），
`response_format: b64_json`、`watermark: false`。错误分类、密钥脱敏、尺寸核对与 88API 相同。不能用于 AI 导演
（`director_providers` 不列出它）。局部编辑只支持「放大局部修」（`crop`，和 Nano Banana 2.1 一样：发两张 1024×1024
的普通图，结果只把标记区域贴回原图）；标记参考、原生圈选和蒙版需要 OpenAI 的编辑字段或要重画整张图，返回 422 `method_unsupported`。

`GET /api/models` 只列出设置里选中的生图服务提供的模型。

Seedream 暂不支持：它的本地参考图每次只能传 1 张，而每个任务至少需要 2 张（新娘、新郎）。

### Settings

```json
{ "default_model": "gpt-image-2", "batch_reference_mode": "prompt_only", "director_model": "gpt-6.1-sol" | null, "portrait_ratio": "3:4", "auto_face_fix": "split" | "ai" | "facefusion" | "off" | "custom", "auto_face_fix_bride": "ai" | "facefusion" | "off", "auto_face_fix_groom": "ai" | "facefusion" | "off", "face_fix_model": "gemini-nano-banana-2.1" | null, "wash_model": "gpt-image-2.5" | null, "face_fix_provider": "88api" | "pptoken" | "ark" | null, "wash_provider": "88api" | "pptoken" | "ark" | null,
  "image_provider": "88api" | "pptoken" | "ark",
  "image_providers": [{"id": "88api", "name": "88API", "available": true, "reason": null}, {"id": "pptoken", "name": "PPTOKEN", "available": false, "reason": "没有配置 PPTOKEN 的密钥：…"}],
  "style_art_mode": "on" | "off" }
```

`style_art_mode`（V7.33）：`on`（默认）时，在用风格的新样片和新照片把这个风格的服饰场景参考图作为最后一张参考图，提示词不再描述服饰和场景物件（docs/prompts.md 第 12 节）；这时整组只在主场景里拍，`batch_reference_mode` 不生效（不发样片）。`off` 与 V7.32 相同。只影响之后新建的任务。

### StyleArt（V7.33 服饰场景参考图）

```json
{ "style_id": "french_couture", "style_name": "…", "outfit_id": "french_flower_hat", "outfit_name": "…",
  "id": "…" | null, "status": "running" | "ready" | "failed" | "interrupted" | null, "model": "…" | null,
  "error_message": "…" | null, "created_at": "…" | null, "finished_at": "…" | null,
  "product_url": "/api/style-arts/{id}/files/product" | null, "scene_url": "…/files/scene" | null, "image_url": "…/files/image" | null,
  "in_use_id": "…" | null }
```

`id` / `status` 等是这个风格最近一次生成；`in_use_id` 是新镜头实际在用的那张（最近一次正在重做或失败时，仍是之前那张）。图片地址优先给在用的那张，没有时给最近一次已做好的步骤。

`image_provider`（V7.11）：生图服务，默认 `88api`。妆容、造型、样片、成片和 AI 修脸的修图请求发给它（AI 导演用 `director_provider` 单独设置）。请求按模型分发：选中的服务提供这个模型就发给它，否则发给唯一提供它的服务（例如切换前排队的 Sunburst 任务、重放旧任务）。`image_providers` 列出每个服务能否选择：PPTOKEN 要在 `config.local.json` 里填 `pptoken_api_key`（地址 `pptoken_base_url`，默认 `https://api.pptoken.cc`），模拟生图模式下都可选。PPTOKEN 与 88API 用同一个客户端：`/v1/images/edits`、同一套尺寸和同一套结果判断，GPT Image 系列的 `quality` 一律为 `xhigh`（2026-11-03 起，此前为 `high`；常量 `OPENAI_QUALITY`），日志里记为 `PPTOKEN`。镜头任务的模型快照记录 `provider`。

`batch_reference_mode`（PRD 3.3）：

| 值 | 拍组任务的参考图 |
|---|---|
| `prompt_only`（默认） | `[新娘, 新郎]`，风格只靠提示词描述 |
| `with_preview` | `[新娘, 新郎, 该样片当前版本]`，提示词注明只参考第三张的风格、不参考其人脸和构图 |

默认 `prompt_only`：样片里的人脸本身是模型的近似结果，作为参考可能让成片偏离本人长相。是否改为 `with_preview` 以实际对比为准。

`auto_face_fix_bride` / `auto_face_fix_groom`（V7.29）：每张有人脸的照片生成后，新娘、新郎各自自动做什么：`ai` AI 修脸，`facefusion` FaceFusion 换脸，`off` 不修。选了 AI 的人先一起做一次 AI 修脸，再由 FaceFusion 换选了换脸的人（docs/prompts.md 第 9.3 节）。`auto_face_fix` 是两人选项对应的搭配名（对不上时为 `custom`）；PATCH 时它仍可一次设好两人，同一请求里的 `auto_face_fix_bride` / `_groom` 在它之后生效。

`face_fix_model`：修手用的生图模型（V7.32 之前也是 AI 修脸用的；V7.32 起 AI 修脸用 `wash_model`，2026-11-13 起用 `face_model`）；`null` 表示跟随 `default_model`。手动修脸请求里的 `model` 优先于它。切换生图服务时，若新服务不提供该模型，重置为 `null`；存储的模型已不在模型列表中时也按 `null` 处理。

`wash_model`（V7.26）：洗图用的生图模型；V7.32～2026-11-12 自动和手动 AI 修脸也用它（V7.32～V7.77 修脸、修手、洗图在同一次 4K 修图里；V7.78 起 AI 修脸只修脸，按配置的尺寸），2026-11-13 起 AI 修脸改用 `face_model`；`null` 表示跟随 `default_model`。洗图请求里的 `model` 优先于它。切换生图服务时，若新服务不提供该模型，重置为 `null`。PATCH 时校验规则与 `face_fix_model` 相同。

`face_fix_provider` / `wash_provider`（V7.27）：修手和洗图各自的生图服务（V7.32～2026-11-12 AI 修脸跟洗图走 `wash_provider`）；`null` 表示跟随 `image_provider`。实际使用的服务和模型按这个顺序决定：这一项自己的服务，否则 `image_provider`；`face_fix_model` / `wash_model` 在这个服务提供时用它，否则用 `default_model`（这个服务提供时），再否则用这个服务的第一个模型。PATCH 时，选的服务没有配置密钥则 409 `provider_unavailable`；模型必须由这一项实际使用的服务提供，否则 422 `invalid_model`。换了服务（这一项自己的，或者它跟随的 `image_provider`）以后，原来的模型如果新服务不提供，重置为 `null`。任务快照记录 `provider`，重试和重放仍发给当时的服务；没有记录的旧任务按当前的 `image_provider`。修脸、修手、洗图请求里的 `model` 也必须由这一项的服务提供，否则 422 `invalid_model`。

每个步骤自己的服务和模型（2026-11-13，用户：每个步骤/阶段的修图模型都可以自定义选择）：除了上面的修手（`face_fix_*`）和洗图（`wash_*`），还有 `face_model` / `face_provider`（AI 修脸，自动和手动）、`makeup_*`（妆容，含新郎修脸照）、`look_*`（造型，含拍摄时按需生成的侧面造型；此前侧面造型用镜头任务的模型）、`style_art_*`（服饰场景图三步；此前用默认模型或镜头任务的模型）和 `regional_*`（局部编辑弹窗预选的服务和模型，只能是能做局部编辑的模型，否则 422 `invalid_model`；弹窗里仍可临时换）。取值、跟随和重置规则与 `face_fix_*` 相同（`db.STAGE_KINDS`，`get_edit_choice`）。妆容、造型和服饰场景图的行只记录模型：建行时取这一步的模型，生成时发给这一步当前的服务（它不提供这个模型时，按模型分发）。升级时 `face_*` 从 `wash_*` 复制一次，AI 修脸继续用原来的服务和模型。

`auto_face_fix`（V7.1，取代 `auto_face_swap`）：生成后自动做什么，`split`（V7.13 起默认，新娘 AI 修脸后新郎 FaceFusion 换脸）、`ai`（两人都 AI 修脸）、`facefusion`（两人都 FaceFusion 换脸，FaceFusion 不可用时不生效）或 `off`（不修）。`split` 在 FaceFusion 不可用时只修新娘，设置页显示不可用原因；不会改用 AI 修新郎。已有的有效设置保留，不覆盖用户选定的模式。见下文「AI 修脸」「分人物自动修脸」「换脸修正」。

`portrait_ratio`：竖版比例，`3:4`（默认）、`2:3` 或 `9:16`；横版使用同一比例横过来（`4:3`、`3:2`、`16:9`）。样片固定为竖版；拍组按每个镜头的 `orientation` 选择。首次启动时取 `config.local.json` 的 `aspect_ratio`（是这三个值之一时），之后以设置为准。只影响之后新建的任务；重新生成、加强长相、换景别沿用原任务快照中的比例。

`director_provider`：AI 导演的文字模型服务，`88api`（默认）或 `pptoken`，和 `image_provider` 互不影响；密钥也分开，读 `config.local.json` 的 `director_api_key` 和 `director_pptoken_api_key`。缺密钥的服务在 `director_providers` 里 `available` 为 false，切换时返回 409 `provider_unavailable`。

`director_model`：AI 导演在当前服务上使用的文字模型名称（任意 `/v1/chat/completions` 可用的模型，由用户填写）；每个服务各记一个，PPTOKEN 默认 `gpt-6.1-sol`。首次启动时取 `config.local.json` 的 `director_model`，没有则为 null。为 null 时拍组和换动作不可用（409 `director_not_configured`）。模拟生图模式下使用模拟导演，不需要配置。

---

## 2. 接口

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/health` | — | `{"ok": true, "generator": "mock" \| "88api", "model": "<当前默认模型>", "face_swap": FaceSwapStatus}` |
| GET | `/api/models` | 可选查询参数 `provider`（`88api` / `pptoken` / `ark`，V7.27） | `Model[]`；指定 `provider` 时列出那个服务的模型，否则列出设置里 `image_provider` 的模型 |
| GET | `/api/settings` | — | `Settings` |
| PATCH | `/api/settings` | `{"default_model"?: string, "batch_reference_mode"?: string, "director_provider"?: string, "director_model"?: string \| null, "portrait_ratio"?: string, "auto_face_fix"?: "split" \| "ai" \| "facefusion" \| "off", "auto_face_fix_bride"?: "ai" \| "facefusion" \| "off", "auto_face_fix_groom"?: "ai" \| "facefusion" \| "off", "face_fix_model"?: string \| null, "wash_model"?: string \| null, "face_fix_provider"?: "88api" \| "pptoken" \| "ark" \| null, "wash_provider"?: "88api" \| "pptoken" \| "ark" \| null, "face_model" / "makeup_model" / "look_model" / "style_art_model" / "regional_model"?: string \| null, "face_provider" / "makeup_provider" / "look_provider" / "style_art_provider" / "regional_provider"?: "88api" \| "pptoken" \| "ark" \| null, "image_provider"?: "88api" \| "pptoken" \| "ark"}` | `Settings`；切换服务时默认模型不是它提供的，改为它的第一个模型；服务没有配置密钥时 409 `provider_unavailable`；`default_model` 或 `face_fix_model` 不在列表中或不是（切换后的）服务提供的时 422 `invalid_model`，参考方式无效时 422 `invalid_reference_mode`，竖版比例无效时 422 `invalid_portrait_ratio`，`director_model` 为空字符串时 422 `invalid_director_model` |
| GET | `/api/styles` | — | `Style[]`（内置风格，固定顺序，见 docs/prompts.md 第 3 节） |
| GET | `/api/style-arts` | — | `StyleArt[]`（V7.33）：每个在用风格一项，按风格顺序；有房间的风格（V7.34）每个房间的每个区域一项，带 `room_id`、`room_name`、`area`、`area_name`、`area_image_url` |
| GET | `/api/styles/{style_id}/rooms/{room_id}/areas/{n}/image` | — | 房间主图（V7.34，仓库里的文件；V7.50 起只有 `n = 1`）；没有这个区域时 404 `asset_not_found` |
| POST | `/api/style-arts/{style_id}/generate` | 有房间的风格必填查询参数 `room`、`area`（V7.34，只重做合成，一次付费生成，白底服饰图共用；缺少或不对时 422 `invalid_room`） | `StyleArt`（202）：用默认模型在后台重新生成这个风格的服饰场景图（三次付费生成，三步全部重做）；做好之前新镜头继续用旧图。风格不存在或已停用时 404 `style_not_found`；正在生成时 409 `style_art_busy` |
| GET | `/api/style-arts/{id}/files/{kind}` | `kind` = `product` / `scene` / `image` | 图片文件；没有这张时 404 `asset_not_found`，文件丢失时 404 `asset_missing` |
| GET | `/api/projects` | — | `ProjectSummary[]`，按 `updated_at` 倒序 |
| POST | `/api/projects` | `{"name"?: string}` | `ProjectDetail`（201），默认名「我的婚纱照」 |
| GET | `/api/projects/{id}` | — | `ProjectDetail` |
| PATCH | `/api/projects/{id}` | `{"name"?: string, "person_a_role"?: Role, "makeup_level"?: "light" \| "natural" \| "refined", "groom_makeup_level"?: "light" \| "natural" \| "refined", "bride_retouch_settings"?: RetouchSettings, "groom_retouch_settings"?: RetouchSettings, "bride_hair_length"?: "long" \| "medium" \| "short", "groom_hair_length"?: "buzz" \| "short" \| "medium"}` | `ProjectDetail`；字段为 null、精细设置不完整或档位无效时 422；选择预设重置对应人的精细设置，同请求显式设置优先 |
| DELETE | `/api/projects/{id}` | — | 204；存在 `running` 任务或正在生成的造型时 409 `project_busy`（先取消剩余任务和排队的造型） |
| POST | `/api/projects/{id}/images` | multipart：`person` = `a`/`b`，`angle` = `front`/`left`/`right`/`left_full`/`right_full`（默认 `front`；V7.48 起 `_full` 是正侧面），`file` | `ProjectDetail`；先检查人脸并裁到头肩（见下文），不合格时 422 `unusable_photo`，无法读取时 422 `invalid_image` |
| DELETE | `/api/projects/{id}/images/{person}/{angle}` | `person` = `a`/`b`，`angle` = `left`/`right`/`left_full`/`right_full` | 204；只清空侧照引用，保留图片资产 |
| GET | `/api/assets/{id}` | — | 图片文件 |
| GET | `/api/look-options` | — | `{"hairstyles": [{"id", "name", "role", "lengths", "updo"}], "makeup_colors": [{"id", "name", "text"}], "styles": {style_id: {"primary", "alternates", "needs_updo"}}}`（docs/prompts.md 第 8 节） |
| PUT | `/api/projects/{id}/style-outfits/{style_id}` | `{"outfit_id": string}` | ProjectDetail；选用这个风格新样片的服饰（V7.17），风格没有这套时 422 `invalid_outfit` |
| POST | `/api/projects/{id}/looks` | `{"client_request_id": string, "person": "a" \| "b", "style_id"?: string, "items": [LookItem]}`（1～16 项；新娘必须给 `style_id`，新郎不能给） | `Look[]`（201；同一 `client_request_id` 再次提交返回原来的候选，200；这个编号已用于别的项目、另一个人或另一个风格时 409 `request_id_reused`）；没有这个人的正面照时 409 `photos_missing`；新娘缺少或给了无效的风格、新郎给了风格、风格已停用且项目没有它的样片时 422 `invalid_style`；发型不适合当前头发长度或这个风格（要求盘发）时 422 `invalid_hairstyle`；新娘在这个风格还没有选用的妆容时（V7.8）、新郎还没有选用的修脸照时（V7.22）409 `makeup_required` |
| POST | `/api/projects/{id}/makeups` | `{"client_request_id": string, "person": "a" \| "b", "style_id"?: string, "count": 1～4}` | `Makeup[]`（201；同一 `client_request_id` 再次提交返回原来的候选，200；编号已用于别的项目、另一个人或另一个风格时 409 `request_id_reused`）。新娘：每张是这个风格的妆容，必须带 `style_id`，风格无效、缺少或已停用（且项目没有它的样片）时 422 `invalid_style`。新郎（V7.22）：每张是所有风格共用的修脸照，不能带 `style_id`，带了时 422 `invalid_style`。没有正面照时 409 `photos_missing` |
| POST | `/api/makeups/{id}/retry` | — | `Makeup`；只有 `failed` / `interrupted` 可以重试，否则 409 `makeup_not_retryable`；照片已更换或是旧版本时 409 `makeup_unusable` |
| POST | `/api/makeups/{id}/select` | — | `Makeup`；替换这个风格原来选用的妆容，已经做好的造型不变；不能使用时 409 `makeup_unusable` |
| POST | `/api/makeups/{id}/reuse` | `{"style_id": string}` | `Makeup`（V7.21）：把另一个风格已经生成好的新娘妆容用于 `style_id`（新郎的修脸照所有风格共用，复用它时 422 `invalid_person`），不再修图、不花钱。目标风格得到一条指向同一张图的妆容记录（已经有同一张图时直接用它，不重复添加），并成为这个风格选用的妆容（`selected_by: "user"`）；这张妆容图上做过的正面发型图（任何风格做的，按图片去重）和它们的侧面图一起复制到目标风格，目标风格不能用的发型（要求盘发的风格）跳过；目标风格的造型没有用户选过时，自动选用最像的一张。妆容不能使用时 409 `makeup_unusable`；风格无效或已停用时 422 `invalid_style` |
| DELETE | `/api/makeups/{id}` | — | 204；隐藏候选；删掉的是选用的那张时，自动选用剩下最像的一张；用它做好的造型不变；还在生成时 409 `makeup_busy` |
| POST | `/api/looks/{id}/retry` | — | `Look`；只有 `failed` / `interrupted` 可以重试，否则 409 `look_not_retryable`；照片已更换或是 V7.0 以前的造型时 409 `look_unusable` |
| POST | `/api/looks/{id}/select` | — | `Look`；替换这个风格原来选用的新娘造型（新郎：替换原来选用的新郎造型）；不能用于新照片时 409 `look_unusable` |
| POST | `/api/looks/{id}/unselect` | — | `Look` |
| DELETE | `/api/looks/{id}` | — | 204；隐藏候选并取消选用，已经用它拍好的照片不变；还在生成时 409 `look_busy` |
| POST | `/api/looks/{id}/side-looks` | — | `SideLook[]`（201，V7.30）：为这套正面造型做侧面造型，这个人每个已上传、还没有侧面造型（或上次失败）的侧脸方向排一张，每张一次修图；返回这套造型的侧面造型。造型没生成好或不能用时 409 `look_unusable`；这个人没有侧脸照时 409 `side_photos_missing`；已有排队中、生成中或做好的不会重复做 |
| DELETE | `/api/side-looks/{id}` | — | 204（V7.24）；隐藏这张侧面造型，下一张需要这个角度的新照片（新拍组、加强长相、换动作、换景别）会重新修一张；已经拍好的照片和它们的「重新生成」仍用原来那张。V7.31 起同一张正面造型图在各风格的这张侧面造型一起隐藏。不存在、已删除或是正面造型时 404 `look_not_found`；还在排队或生成时 409 `look_busy` |
| POST | `/api/projects/{id}/previews` | `{"client_request_id": string, "style_ids"?: string[], "model"?: string}` | `Batch`（201）；`model` 缺省用默认模型，不在列表中时 422 `invalid_model`；某个风格还没有选用的新娘造型或新郎造型时 409 `looks_required`（V7.0 起不再接受 `looks`，带上会 422） |
| POST | `/api/projects/{id}/moment-drafts` | `{"preview_slot_id": string, "count": 4 \| 8 \| 12 \| 20}` | `MomentDraft`（201）；同步调用 AI 导演 |
| GET | `/api/moment-drafts/{id}` | — | `MomentDraft` |
| PATCH | `/api/moment-drafts/{id}/moments/{index}` | `{"title"?: string, "shot_size"?: string, "orientation"?: string, "bride_facing"?: string, "groom_facing"?: string, "text"?: string, "faces_clear"?: boolean}` | `MomentDraft`；用户手动修改第 `index`（从 0 开始）个瞬间 |
| POST | `/api/moment-drafts/{id}/moments/{index}/rewrite` | `{"shot_size"?: string}` | `MomentDraft`；请 AI 导演只重写这一个。不传 `shot_size`：换一个新瞬间；传入：保持该瞬间的动作和画幅，改写为指定景别 |
| POST | `/api/projects/{id}/photo-batches` | `{"client_request_id": string, "draft_id": string, "model"?: string}` | `Batch`（201，`batch_type = "photo"`），张数 = 方案中瞬间的数量 |
| GET | `/api/projects/{id}/batches` | — | `Batch[]`，按创建时间倒序 |
| GET | `/api/batches/{id}` | — | `Batch` |
| POST | `/api/batches/{id}/pause` | — | `Batch`（V7.12：`paused` 置为 true，`waiting` → `held`；正在生成的继续完成，它们排出的自动修脸或换脸任务也以 `held` 排队） |
| POST | `/api/batches/{id}/continue` | — | `Batch`（`held` → `waiting`，`paused` 置为 false；用户暂停的和重启、失败后搁置的都恢复） |
| POST | `/api/batches/{id}/cancel` | — | `Batch`（`waiting`/`held` → `cancelled`；正在生成的继续完成） |
| POST | `/api/batches/{id}/stop` | — | `Batch`（V7.12：`waiting`/`held` → `cancelled`，`running` → `cancelled` 且 `error_code = "stopped"`；请求已经发出，服务商停不下来，可能已经扣费，结果回来后丢弃，不生成版本） |
| POST | `/api/slots/{id}/regenerate` | `{"client_request_id": string, "mode": "same" \| "identity_boost", "model"?: string}` | `Batch`（201，`batch_type = "regenerate"`） |
| POST | `/api/slots/{id}/change-pose` | `{"client_request_id": string, "model"?: string}` | `Batch`（201）；仅 `kind = "photo"` |
| POST | `/api/slots/{id}/change-shot` | `{"client_request_id": string, "shot_size": "wide" \| "full" \| "three_quarter" \| "half" \| "close", "model"?: string}` | `Batch`（201）；仅 `kind = "photo"`；停用景别 `medium`、`extreme_close`、`detail` 不能作为目标（422） |
| POST | `/api/slots/batch-action` | `{"client_request_id": string, "slot_ids": string[], "action": "same" \| "identity_boost" \| "change_pose" \| "change_shot" \| "face_swap" \| "face_fix", "shot_size"?: string, "model"?: string}` | `Batch`（201），一个批次内每个位置一个任务；`action = "change_shot"` 时必须提供 `shot_size`；`action = "face_swap"` 时忽略 `model`；`action = "face_fix"` 时 `model` 是修图用的模型 |
| POST | `/api/slots/{id}/face-swap` | `{"client_request_id": string}` | `Batch`（201，`batch_type = "regenerate"`）；样片与相册作品均可 |
| POST | `/api/slots/{id}/face-fix` | `{"client_request_id": string, "model"?: string}` | `Batch`（201，`batch_type = "regenerate"`）；AI 修脸（V7.1，V7.2 起修整张照片），样片与相册作品均可；校验见下文，不需要 FaceFusion |
| POST | `/api/slots/{id}/hand-fix` | `{"client_request_id": string, "model"?: string}` | `Batch`（201，`batch_type = "regenerate"`）；修手（V7.25），样片与相册作品均可，局部特写也可以；校验见下文 |
| POST | `/api/slots/{id}/wash` | `{"client_request_id": string, "model"?: string}` | `Batch`（201，`batch_type = "regenerate"`）；洗图（V7.26），按 4K 输出，样片与相册作品均可；校验见下文 |
| GET | `/api/slots/{id}/versions` | — | `VersionDetail[]`，按 `version_number` 倒序 |
| POST | `/api/slots/{id}/restore` | `{"version_id": string}` | `Slot` |
| DELETE | `/api/slots/{id}` | — | 204；仅 `kind = "photo"`；存在 `running` 任务时 409 `slot_busy` |
| POST | `/api/projects/{id}/export` | `{"slot_ids"?: string[]}` | `application/zip`（附件下载），缺省为全部有版本的作品 |
| GET | `/api/tasks` | 查询参数 `project_id`、`batch_id`、`target_slot_id`、`status`（均可选） | `Task[]`，按创建时间倒序；`target_slot_id` 用于查询同一张照片的全部历史任务，不能只根据最新任务判断是否占用 |
| POST | `/api/tasks/{id}/retry` | — | 新 `Task`（201）；仅 `failed`/`interrupted` 可重试；批次暂停时新任务为 `held`，否则为 `waiting` |
| POST | `/api/tasks/{id}/cancel` | 可选查询参数 `expected_status`（`TaskStatus`） | `Task`；`waiting`/`held` → `cancelled`；V7.12 起 `running` 也可以中断：→ `cancelled`，`error_code = "stopped"`，结果回来后丢弃；其他状态 409 `task_not_cancellable`。提供 `expected_status` 时，在同一事务内先核对状态，不匹配则 409 `task_state_changed` 且不取消任务；局部编辑器使用 `expected_status=held`，防止误停已经恢复运行的任务 |
| POST | `/api/tasks/{id}/pause` | — | `Task`（V7.12）；`waiting` → `held`；其他状态 409 `task_not_pausable` |
| POST | `/api/tasks/{id}/continue` | — | `Task`（V7.12）；`held` → `waiting`；其他状态 409 `task_not_held`；整批已暂停时 409 `batch_paused`，须先继续整批任务 |

**上传照片的预处理**（`backend/app/photo_prep.py`）：生图模型会把参考图缩到约 1024 像素再看，手机全身照里的脸只剩百余像素，长相还原差；所以上传时先用 YuNet 人脸检测（OpenCV，模型在 `backend/app/assets/`，MIT 许可）检查，再裁到头肩。

- 拒绝（422 `unusable_photo`，`message` 为原因）：没有检测到人脸；有另一张面积不小于主脸 25% 的脸（照片里不止一个人）；主脸的短边小于 120 像素（太小或太模糊）。远处路人的小脸不算。
- 裁剪：以主脸为中心，宽 3 个脸宽，脸上方 0.7 个脸高、下方 1.3 个脸高，超出照片的部分不补边；长边超过 2048 像素时缩小。按 EXIF 方向转正，透明图保存为 PNG，其余为 JPEG。
- 不做 AI 放大或人脸修复（会改变长相），也不抠图换背景。
- 原样的上传另存为 `asset_type = 'upload'`；裁好的照片是 `asset_type = 'original'`，`source_asset_id` 指向原样的上传。项目引用、生图和换脸都用裁好的照片。在此以前上传的照片没有裁剪，保持原样。

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
- **生成样片**：两张照片齐全才允许，否则 409 `photos_missing`。`style_ids` 缺省为默认风格（`Style.default_preview` 为 true 的前 6 种）；前端在用户未选风格时不传该字段，「重新生成全部」只传已有样片的风格。每种风格复用该项目已有的样片位置（没有则新建），所以重生样片会产生新版本。每个风格用 `ProjectDetail.style_looks` 里的造型（V7.0）。
- **妆容（V7.8，docs/prompts.md 第 8.1 节）**：新娘每个风格先生成妆容候选并选用一张，这个风格的新娘造型都是在它上面只改发型（一次付费编辑），不再每张自己化妆。妆容候选和造型候选一样排在拍摄任务前面（妆容先于造型），后端重启时的处理也相同；删除项目时还在排队的妆容取消，正在生成的要等它结束。
- **造型（V5.0，docs/prompts.md 第 8 节）**：造型候选由用户点按钮生成，每张是一次付费编辑（从这个人上传的正面照修一次，图片类型 `styled`，`source_asset_id` 指向上传照片），在任务队列里排在拍摄任务前面；后端重启时正在生成的候选变为 `interrupted`，还在排队的变为 `failed`（`not_started`），都不会自动重发。V7.0 起造型放在选风格之后：新娘每个风格做造型（`style_id`），一次修图做好这个风格适合的发型和这个风格的妆容（主推色系和妆面），新郎的造型所有风格共用；修图走项目当前的生图模型，编辑结果原样保存，不带遮罩、不贴回原脸（V6.x 的遮罩和 PPTOKEN 已删除，`pptoken_api_key` 不再读取）。每个风格只选用一套新娘造型，新郎只选用一套；第一套生成好的自动选用，之后选别的会替换。生成样片时直接用这个风格选用的造型，没有就 409 `looks_required`；样片和照片的提示词照着造型照的妆拍，不再写档位。镜头任务在模型快照里记录 `looks`（`{"version": 4, "bride": look_id, "groom": look_id}`；V6.x 为 3，V5.x 为 2）和每张参考图是谁的哪个角度 `reference_slots`；正面参考图就是两人造型的图片。拍组沿用样片的造型，加强长相、换动作、换景别沿用来源作品的造型；来源作品没有记录造型（V5.0 以前）时 409 `looks_required`，造型已不能用于新照片时 409 `look_unusable`。镜头需要某人的侧面参考时，任务开始生成时按这套造型的发型和妆容编辑侧面照，得到一张侧面造型（V7.46 起只发侧面照；同一套造型、同一角度、同一张侧面照只编辑一次，文件丢失时重新编辑），并写回任务的 `reference_image_ids`；编辑失败按生成失败的同一套规则处理，不退回用原侧面照。作品的「重新生成」原样重放任务记录的参考图。换脸用作品自己的参考图：正面是它的造型，用到的侧面是它的侧面造型，其他侧面优先用这套造型的侧面造型，没有就用原侧面照；V4.0 以前的作品用项目当前的上传照片。V4.0 的任务没做完造型照就重试时失败，`error_code` 为 `styling_retired`。服饰场景参考图（V7.33，docs/prompts.md 第 12 节）：`style_art_mode` 为 `on` 时，在用风格的新镜头（样片、拍组、加强长相、换动作、换景别）在参考图最后加这个风格的服饰场景图，模型快照记录 `style_art: {"style", "outfit", "version"}`，`reference_slots` 末尾多一个 `null`。图已做好时建任务时就放进 `reference_image_ids`；还没做好时任务开始生成时先做（三次付费生成，同风格只做一张，等着的任务共用），再写回 `reference_image_ids`，所以「重新生成」原样重放。图是所有项目共用的文件（`data/style_art/<id>/`），每个项目第一次用到时添加一条指向它的 `image_assets`（`asset_type = 'style_art'`），删除项目或作品不会删掉共用文件。做图失败按生成失败处理，记录保留做好的步骤，下次接着做。参考图张数超过模型上限时与其他参考图一样 422 `invalid_model`。后端重启时正在做的图标为 `interrupted`。人脸相似度：造型与上传照片比较（`face_score`）；样片和作品（V7.33 起）与生成它所用的正面造型照比较，衡量「画面把造型还原得多像」，换脸程序按造型照测出的前后分数直接记录。V7.33 以前打的分是与上传照片比较的，已有的分数不重算；V4.0 以前的作品本来就用上传照片生成，仍与上传照片比较。
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
  - 新任务记录 `regenerate_mode`（SQLite `generation_tasks.regenerate_mode` 允许 `NULL`、`same`、`identity_boost`、`change_pose`、`change_shot`、`face_swap`、`face_fix`、`hand_fix`）；首次生成和既有任务的手动重试默认保留原有操作类型。成功后新增版本并设为当前版本；失败不影响当前版本。
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
  - 输入版本：当前版本；若当前版本本身是换脸或 AI 修脸的结果，改用它的 `source_version_id` 指向的版本（来源版本已不存在时 409 `slot_not_ready`）。任务记录 `source_version_id`。
  - 任务字段：`regenerate_mode = "face_swap"`；`reference_image_ids = [新娘正面, 新郎正面, 新娘左, 新娘右, 新郎左, 新郎右]`（项目当前照片，没有的侧面照为 null；自动换脸同样取项目当前照片；2026-10-28 之前的任务只有两张正面）；`style_id`、`pose_id`、`moment_title`、`moment_shot_size` 复制自输入版本的任务，所以成功后作品的动作名与景别不变；`prompt` 为空字符串；`model_config_snapshot = {"generator": "facefusion" | "mock", "model": "hyperswap_1c_256/hyperswap_1b_256", "front_model": "hyperswap_1c_256", "side_model": "hyperswap_1b_256", "side_turn": 0.18, "pixel_boost": "512x512", "face_mask_types": ["box", "occlusion"], "weight": 0.5}`；换脸成功后追加 `faces`（每人的 `turn`、`facing`、`model`、`sources`，侧面照里检测不到对应性别的脸时另有 `side_photo_unusable: true`），`model` 改为实际的「新娘/新郎」模型（相册作品和样片都是 0.5；2026-11-03 之前的样片任务记录的是 1.0）。其中 `model` 是界面显示的版本模型摘要。
  - 执行（FaceFusion）：一个进程内完成检测与两次换脸。先检测新娘、新郎原照的性别，不符时失败 `roles_swapped`（「新娘和新郎的照片可能放反了，请在工作台点『交换』后重试」）；再检测输入图片，缺少女性脸或男性脸时失败 `face_not_found`（「没有识别到新娘的脸，可能是侧脸角度过大或被遮挡」，新郎同理，两者都缺时合并说明）。然后按每人在输入图片里的转头程度选模型和源照片（规则见 PRD 4.5：正脸 `hyperswap_1c_256` + 正面照，侧脸 `hyperswap_1b_256` + 正面照与对应侧面照），先换女性脸、再换男性脸。实际用到的模型均须在 profile 中确认执行过 `CUDAExecutionProvider` 运算，否则任务失败。模型是用户在 2026-10-27 对真实照片比较后选定的；`512x512` 像素提升当时直接采用，2026-10-28 与 `256x256` 对比后保留；遮罩 `box` + `occlusion` 同日选定；相册强度同日改为 `0.5`；样片强度 2026-10-28 定为 `1.0`，2026-11-03 改回 `0.5`（见 PRD 4.5）。强度定义在后端常量 `FACE_SWAP_WEIGHT`，创建任务时写入快照，执行时按快照传给 FaceFusion，所以旧任务重新执行仍用当时的强度。
  - 失败处理：`roles_swapped`、`face_not_found`、`reference_missing` 直接失败，不自动重试；FaceFusion 进程崩溃、超时（120 秒）或没有输出文件为 `face_swap_failed`（「换脸处理失败，请稍后重试」），自动重试最多 2 次。
  - 调度：换脸任务单独一条队列，同时最多运行 1 个；不计入生图并发 `max_concurrency`，不受 429 退避影响，鉴权/余额搁置也不影响它。
  - 启动恢复：`running` 的换脸任务改回 `waiting`（本地、不收费，可安全重做），`waiting` 的换脸任务保持 `waiting`，不改为 `held`。
  - 成功后新增版本并设为当前版本；失败不影响当前版本。`is_stale` 照常计算。
  - **自动换脸**：设置 `auto_face_fix = "facefusion"`（V7.1 起不再是默认）且 FaceFusion 可用时，任何非换脸任务成功保存版本的**同一个事务**里，再插入一个换脸任务：同一批次、同一位置，`source_version_id` 为刚保存的版本，`reference_image_ids` 为该生图任务的前两张参考图（生成时用的新娘、新郎照片），其余字段同上。换脸任务本身完成后不会再触发换脸。
  - **对换脸版本的其他操作**：重新生成、加强长相、换动作、换景别以及导出时读取的「来源任务」，会沿 `source_version_id` 追溯到生成该图的生图任务，而不是换脸任务（换脸任务的 `prompt` 为空）。
- **AI 修脸（face-fix，V7.1；V7.2 起修整张照片，docs/prompts.md 第 9 节）**：
  - 一次编辑请求，发送 `[要修的照片, 新娘造型照, 新郎造型照]`（V7.14 起；V7.9～V7.13 新娘用改发型之前的化妆图；V7.4～V7.8 两人都用造型照；此前是上传的正面照），用 `face_fix` 提示词（88API，作品自己的比例，不带蒙版），结果整张保存为新版本并设为当前版本。每张照片一次付费修图。不需要 FaceFusion。
  - 请求校验：该位置没有当前版本时 409 `slot_not_ready`；有 `waiting`/`running`/`held` 任务时 409 `slot_busy`；照片缺失时 409 `photos_missing`；局部特写 409 `no_faces`；`model` 不在列表中时 422 `invalid_model`。输入版本是当前版本，当前版本本身是换脸或 AI 修脸的结果时，改用它的来源版本。
  - 任务字段：`regenerate_mode = "face_fix"`；`reference_image_ids = [新娘正面造型照, 新郎正面造型照]`（由输入版本任务的前两张参考图得出；V4.0 以前的作品是上传照片）；`prompt` 为空字符串（新任务用固定的 `FACE_FIX_PROMPT`，V7.32～V7.77 在最后一段前插入修手和洗图两段，V7.78 起不再插入；已记录 `bride_reference = "makeup"` 的旧任务仍用 `FACE_FIX_MAKEUP_PROMPT`）；`model_config_snapshot = {"generator": "face_fix", "model": 修图模型, "aspect_ratio": 输入版本的比例, "bride_reference": "look", "provider": 服务}`（V7.78 起按配置的尺寸输出；V7.32～V7.77 的任务还记录了 `retouch: ["hands", "wash"]` 和 `resolution: "4K"`，重试时仍修手、洗图并按 4K。修图模型取请求里的 `model`，否则 `face_model`（2026-11-13 之前是 `wash_model`），再否则默认模型）（V7.9～V7.13 的任务可能是 `makeup`；V7.9 以前的任务没有这个字段，按 `look` 处理）。
  - 失败处理与生成任务相同（含自动重试、`interrupted` 和余额搁置），失败不影响当前版本。
  - 验收（V7.5）：修好的版本和它的来源版本都有 `face_scores` 后，新娘或新郎任意一人比来源低 0.02 以上（或修后为 null 而来源有分数），作品的当前版本换回来源版本，修好的版本留在历史里，任务快照追加 `"kept_original": ["bride" | "groom", …]`。没有 FaceFusion 或分数不全时不判断。调度：和生成任务同一条队列，计入 `max_concurrency`；启动恢复时按生成任务处理（`running` 改为 `interrupted`，`waiting` 改为 `held`），因为修图会扣费。
  - **自动修脸**：设置 `auto_face_fix = "ai"` 时，任何生成任务成功保存版本的同一个事务里，插入一个 AI 修脸任务（同一批次、同一位置，`source_version_id` 为刚保存的版本）；`"facefusion"` 时改为插入换脸任务（需要 FaceFusion 可用，见上文）；`"split"` 时按下述分人物流程处理。细节特写、背影和剪影镜头不插入。修脸和换脸任务完成后不会再触发新一轮自动修脸。重新生成、加强长相等操作读取「来源任务」时，同样沿 `source_version_id` 越过 AI 修脸任务。
- **修手（hand-fix，V7.25，docs/prompts.md 第 10 节）**：
  - 一次编辑请求，只发送 `[要修的照片]` 和修手提示词；模型取请求里的 `model`，否则设置的 `face_fix_model`（修手模型），再否则默认模型。
  - 输入版本是作品的**当前版本**（做过 AI 修脸或换脸的，就在修正后的那张上修），不像 AI 修脸那样追溯到最初的生成版本；之后再做 AI 修脸，从修好手的版本开始。
  - 请求校验：没有当前版本时 409 `slot_not_ready`；有 `waiting`/`running`/`held` 任务时 409 `slot_busy`；模型无效时 422 `invalid_model`。
  - 任务字段：`regenerate_mode = "hand_fix"`；`reference_image_ids` 是输入版本任务的前两张参考图（两人的正面造型照），不发送，只用于打相似度分；`style_id`、`pose_id`、`moment_*` 和画面比例取自最初的生成任务；`model_config_snapshot = {"generator": "hand_fix", "model", "aspect_ratio"}`。
  - 不会自动排在别的任务之后，修手的结果也不会再触发自动修脸。验收同 AI 修脸：任一人的相似度比来源低 0.02 以上时退回来源版本，快照记录 `kept_original`。
  - 重新生成、加强长相、换动作、换景别和导出读取的「来源任务」会越过修手版本，追溯到最初的生成任务。
- **洗图（wash，V7.26，docs/prompts.md 第 11 节）**：
  - 与修手相同的输入、校验、记录、验收和来源追溯，区别只有三点：提示词是洗图提示词；这一次请求按 4K 尺寸要（快照 `{"generator": "wash", "model", "aspect_ratio", "resolution": "4K"}`）；模型取请求里的 `model`，否则设置的 `wash_model`，再否则默认模型。
  - 不会自动排在别的任务之后，洗图的结果也不会再触发自动修脸。
- **分人物自动修脸（V7.13，`auto_face_fix = "split"`，默认）**：
  - 先排一个 `face_fix` 任务，快照追加 `roles: ["bride"]`、`then: "groom_swap"`；实际只发送 `[要修的照片, 新娘造型照]`，使用只修新娘的提示词，新郎保持原样。仍记录两人的参考图，供相似度评分使用。
  - 保存新娘 AI 修图版本的同一个事务里，FaceFusion 可用时创建只换新郎的 `face_swap` 任务（同批次、同位置，快照 `roles: ["groom"]`），并在 AI 任务快照记录 `groom_swap_queued: true`。因此评分期间批次仍未完成，暂停、中断、重启都能识别这个后续任务。
  - 新郎任务先在换脸通道中为原图和 AI 修图评分，已测过的分数保留。验收只看新娘：相似度降低超过 0.02 或原本能识别、修后无法识别时，退回原图；AI 修图留在历史版本。然后更新新郎任务的 `source_version_id`，只在保留的图片上换新郎。评分失败不阻断换脸，保留 AI 修图；作品已切换到其他版本时取消这次自动换脸（`source_changed`）。
  - 新郎使用这张作品原本的新郎造型照和适用的侧面照，按目标转头程度选模型；样片强度 1.0，相册 0.5。只要求目标图能识别到新郎，`faces` 和成功后的 `model` 只记录新郎；两个人的分数仍照常返回。
  - 批次暂停时后续任务以 `held` 排队；重启后仍保持暂停。新郎失败后重试只重做本地换脸，不重发付费的新娘 AI 修图。新娘 AI 修图失败时不创建新郎任务，原图保留。未配置 FaceFusion 时只生成新娘 AI 修图版本。
  - 照片菜单里的「AI 修脸」「换脸修正」仍分别处理两人；从分步结果再次手动修正时，沿完整的 `source_version_id` 链找到最初的生成版本及其造型。
- **数据库升级**：启动时通过 `PRAGMA table_info` 检查 `generation_tasks.regenerate_mode`。升级保留现有任务和版本数据；为使删除作品后仍能保留任务行，任务表升级时会以事务复制原有列、增加可空的 `regenerate_mode`，并解除任务目标位置的级联外键；删除后 `target_slot_id` 继续保存原位置 ID 供追溯。

## 房间（V7.34，docs/prompts.md 第 13 节）

- `GET /api/styles` 每项多 `rooms: [{id, name, areas: [{name, image_url}]}]`；没有房间的风格为空数组。目前只有法式复古有房间。
- `POST /api/projects/{id}/previews` 可带 `rooms: {style_id: room_id}`；有房间的风格不带时用第一个房间，房间不对时 422 `invalid_room`。样片拍在区域 1。
- V7.56：每个风格的每个房间各有一张样片（之前每个风格只有一张，换房间会覆盖）。`rooms` 的值也可以是房间数组 `{style_id: [room_id, ...]}`，每个房间生成一张样片；已有样片的房间生成新版本，没有的新建。空数组、重复或不存在的房间 422 `invalid_room`。`GET /api/projects/{id}` 的 `previews` 按风格顺序、再按房间顺序排列，每项的 `room` 是它的房间。拍组照（带样片参考时）用的是同风格同房间的那张样片。旧数据库升级时，已有样片按最近一次生成的房间补上。
- 镜头任务的模型快照记录 `room: {"id", "area"}`；有服饰场景参考图时 `style_art` 也带 `room`、`area`。拍组、导演方案（新建和改写）用样片的房间，导演在房间的区域里选 `scene`；方案的区域编号超出样片房间时建组 409 `draft_stale`。加强长相、换景别沿用原镜头的区域，换动作由导演重新选。没有记录房间的旧作品照旧用风格的场景变体。
- `Slot` 多 `room: {id, name, area, area_name} | null`：当前版本拍在哪个房间、哪个区域。

## 样片页面的服饰场景参考图（V7.35）

- `GET /api/slots/{slot_id}/style-arts` → `StyleArt[]`：用这张样片拍一组时要用的服饰场景参考图，按样片记录的服饰；有房间的风格是这个房间每个区域一项，否则是这个风格的一张。设置关闭（`style_art_mode = off`）或样片不是用它拍的时返回空数组；不是样片时 409 `slot_not_preview`。拍组窗口里显示这些图，用户可以先检查、提前生成或重新生成。
- `StyleArt.cost`：现在生成（或重新生成）这张要几次付费生成。房间区域是 1；这套服饰还没有白底服饰图时是 2；没有房间的风格是 3。
- `POST /api/style-arts/{style_id}/generate` 可带 `outfit`（这个风格的服饰之一，默认第一套；不对时 422 `invalid_outfit`）。
- `Slot.style_art_asset_id`：当前版本用的服饰场景参考图（图片资源，用 `/api/assets/{id}` 取）；没有时为 null。样片卡片上显示它。
- 成组照片不发样片作参考图（V7.33 起，服饰场景参考图打开时）；样片用来检查人像、妆容和衣服上身的效果，并决定这一组的造型、服饰和房间。
- V7.37：`POST /api/projects/{id}/previews` 可带 `outfits: {style_id: outfit_id}`，和房间一起在生成样片的确认框里选；同时写成项目在这个风格的服饰选择（与 `PUT /api/projects/{id}/style-outfits/{style_id}` 相同）。服饰不属于这个风格时 422 `invalid_outfit`。不带的风格用项目当前的选择。


## 区域图（V7.38，V7.50 起取消）

- V7.50 起房间只有主图（区域 1）一张图：`GET /api/styles` 里房间区域的 `image_url` 只有区域 1 有，其他为 null；`GET /api/styles/{style_id}/rooms/{room_id}/areas/{n}/image` 只接受 `n = 1`，其他 404 `asset_not_found`。
- 生成区域图的接口已删除。服饰场景参考图每个房间一张（`area` 为 1），`POST /api/style-arts/{style_id}/generate` 的 `area` 可以不传（传了也按 1）。


## 局部编辑（2026-11-08）

### 查询编辑选项

`GET /api/regional-edit/options?provider=88api|pptoken` 返回 `models: Model[]` 和 `methods`。
每种方式包含 `id`、`name`、`available`、`description`。省略服务时使用当前 `image_provider`。
`GET /api/regional-edit/models` 接受同一个参数，只返回模型列表。
目前列出 GPT Image 2.5、2.6 系列，不改变普通生成的模型列表或默认模型。
Nano Banana 2.1（`gemini-nano-banana-2.1`，2026-11-11，仅 88API）也列出，但**只能用 `crop`**：`crop` 只发两张普通图片（1:1、1K），Gemini 能接收；`markup`、`mask` 是 OpenAI 图片编辑接口特有的文件字段，Gemini 的聊天接口没有；`reference` 要重画整张照片，人脸会被重新生成。
`methods` 每项另有 `models: string[]`，列出能用这种方式的模型 id；Nano Banana 2.1 只出现在 `crop` 里。用它提交其他方式返回 422 `method_unsupported`，不创建任务、不计费。

- `crop`（V7.79，界面默认，排在第一位）：放大局部修。标记是涂满的红色区域（不是圈线）。后端以标记区域为中心裁一块正方形（边长为标记长边的 2 倍，至少 320 像素，不超出原图），放大到 1024×1024，连同一张标出红色半透明区域的同尺寸副本发给模型（普通编辑请求，`size=1024x1024`，质量按配置）；结果缩回裁剪尺寸后，只把标记区域（稍微外扩、边缘羽化）贴回原图。标记以外的像素和原图尺寸都不变。原因：GPT 图片编辑即使带蒙版也会重画整张图，手只占画面约 1%，像素太少画不好手指，脸也会被重画，结果还被缩小（实测 1536×2048 变 1086×1448）。
- `reference`：标记参考（兼容）。干净原图是第一张输入；第二张是叠加标记的参考副本，仅用于定位。不会向上游发送 `markup` 或 `mask` 文件字段。
- `markup`：原生独立透明标记图。原图使用 `image`，标记使用 `markup`；不会把标记合并进原图。
- `mask`：透明蒙版。原图使用 `image`，蒙版使用 `mask`，完全透明区域表示要编辑的位置。

实测记录：PPTOKEN 在 2026-11-08 拒绝 `markup`，禁用这个选项，直接提交返回 422 `markup_unsupported`。
PPTOKEN 接受 `mask`，但九宫格样例未正确遵循区域，因此界面保留效果提示；同一合成图的 `reference` 方式正确修改圈内对象。
88API 因账户余额不足，未完成原生标记效果验证。接口受理不等于编辑效果正确，不会在失败后自动改用其他方式或服务。

### 手部诊断与候选定位（2026-11-09）

`POST /api/slots/{slot_id}/hand-analysis`，请求为 `multipart/form-data`：

| 字段 | 说明 |
| --- | --- |
| `source_version_id` | 必填，分析时的当前照片版本；请求前后均检查版本是否改变 |
| `annotation_kind` | `crop`（默认）、`reference`、`markup`、`mask`，决定如何理解标记 |
| `instruction` | 可选，最多 2000 字符，用户想修改的目标；只作为待核查提示，不当作已确认的图像证据 |
| `annotation` | 可选，同原图方向校正后尺寸的透明 PNG；提供时诊断圈选目标，不提供时查找整图候选 |

使用当前 `director_provider`、`director_model` 和该服务的独立 AI 导演密钥，单次 `/v1/chat/completions` 看图请求。
需使用支持图片输入的模型，未配置时返回 409 `hand_analysis_unavailable`；服务拒绝或连接失败返回 502 `hand_analysis_failed`；
无效 JSON、坐标越界、状态冲突或缺乏证据的修复指令返回 502 `hand_analysis_invalid_output`，不重试、不切换模型、不创建修图任务。
模拟模式只返回 `uncertain`，不伪造看图结果。分析独立计文字模型费用；版本在分析期间改变时返回 409 `source_changed`，请求可能已经计费。

响应包含 `source_version_id`、`scope`（`selection` / `detect`）、`model`、`provider`、`image_size`、`created_at`，以及：

- `status`：`repair` / `reconstruct` / `normal` / `uncertain`；`summary` 为结论或建议。`repair` 为可见缺陷的定向修复，
  `reconstruct` 为有可见姿态依据但无法确认完整指节归属时的建议重建，不代表已确认畸形；`uncertain` 为仍无法提出有依据的方案。
- `regions`：最多四个候选；圈选诊断最多一个（交叠的手合并）。每项包含 `label`、`status`（`repair` / `reconstruct` / `uncertain`）、
  `bbox`（相对方向校正后完整原图的 `[x0,y0,x1,y1]`，各值 0～1）、`evidence`、`problems`、`goal`、`preserve`、`uncertainties`。
- `repair_prompts`：与 `regions` 一一对应，由后端按诊断或建议重建目标生成，每项不超过 2000 字。
  `reconstruct` 的提示词明确标注为建议，不冒充确定的诊断，需用户选择采用；`uncertain` 对应空字符串。
- `annotation_sha256`、`annotation_kind`：圈选诊断绑定规范化后的原始标记；整图检测时均为 null。
- `selected_region_index`：响应为 null；采用某个 `repair` 或 `reconstruct` 方案时，客户端设为该候选下标并随局部编辑提交。不能直接采用 `uncertain`。

完整原图用于判断人物和动作；圈选时另发送 1024×1024 干净局部图及标记副本。
图像在发送前校正方向并移除元数据，坐标不受缩放影响。定位只是模型建议，需人工检查，不能视为精确分割或已验证的畸形检测。
整图初筛的 `uncertain` 候选也可点击「圈选并放大分析」，客户端用候选坐标生成 `crop` 标记后，再发一次带 `annotation` 的局部分析请求。
这一步明确计一次额外文字模型请求，不自动循环或修图。不能因为遮挡部分归属未知而放弃修复已经可见的缺陷。

### 提交局部编辑

`POST /api/slots/{slot_id}/regional-edit`，请求为 `multipart/form-data`：

| 字段 | 说明 |
| --- | --- |
| `client_request_id` | 必填，1～128 字符，同一次提交及网络重试复用这个编号 |
| `source_version_id` | 必填，编辑器中显示的当前版本 |
| `prompt` | 必填，去除首尾空白后非空，最多 2000 字符 |
| `hand_analysis` | 可选，诊断响应的 JSON 字符串（最多 20000 字符），填入 `selected_region_index`；写入任务快照。来源版本、尺寸、诊断结构与候选状态必须有效；圈选诊断还检查标记摘要和编辑方式，不符返回 422 `invalid_hand_analysis`。实际修图要求仍以 `prompt` 为准，允许用户修改。 |
| `provider` | 必填，`88api` 或 `pptoken`，明确选定本次服务 |
| `model` | 必填，必须是该服务提供的局部编辑模型；Nano Banana 2.1 只能配 `method=crop` |
| `method` | `native`（默认）、`reference` 或 `crop`（V7.79，界面默认）；兼容方式必须明确选择 `reference` |
| `markup` / `mask` | 必须且只能上传一个带透明通道的 PNG。`reference` 方式使用 `markup` 字段上传圈线层，`crop` 方式使用 `markup` 字段上传涂满的标记区域 |

原照片从 `source_version_id` 的资源读取，客户端不重复上传原图。
标记尺寸必须匹配应用 EXIF 方向后的原图；每张最多 16777216 像素，标记文件最多 50 MB。
拒绝空标记、全透明、全不透明、尺寸不符、无透明通道和损坏的文件；绘制层的元数据在保存前移除。
`reference`、`markup`、`mask` 三种方式使用 `quality=medium`、`size=auto`、`output_format=png`；`crop` 快照记录 `size: "1024x1024"`，结果按原图尺寸保存。结果均校验并保存为 PNG。
这些参数只作用于局部编辑，不覆盖普通生成或洗图的质量、尺寸。

成功返回 `201 Batch`。相同编号、内容和目标再次提交返回 `200`，不重复付费；
编号被其他内容占用时返回 `409 request_conflict`。原图版本已变更时返回 `409 source_changed`；
照片已有排队、暂停或运行中的任务时返回 `409 slot_busy`。失败保留原图，不静默去掉标记重试。

任务的 `regenerate_mode` 为 `regional_edit`，`prompt` 保存用户要求，`source_version_id` 保存原图版本。
模型快照记录服务、模型、编辑方式、标记资源、原图尺寸与参数。暂停、继续、中断、重启恢复及手动重试沿用现有任务规则；
重试沿用原标记和快照，不使用后来变更的设置。标记资源类型为 `edit_crop`、`edit_reference`、`edit_markup` 或 `edit_mask`。
删除作品时一并删除其标记资源；删除项目时随项目清理。

结果保存为可对比、恢复的历史版本。若编辑期间用户恢复了其他版本，晚到的结果只保存到历史，不覆盖用户恢复的版本。
局部编辑不会追加自动 AI 修脸或换脸任务，避免随后整图编辑覆盖圈选修改。
「重新生成」「换动作」「换景别」等拍摄操作仍追溯到原拍摄任务，而不是把局部编辑指令当作拍摄提示词。
