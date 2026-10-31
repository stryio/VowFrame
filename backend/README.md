# VowFrame 后端

使用 Python 3.14，在 backend 目录运行：

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
.venv/bin/python -m pytest -q
```

依赖见 `requirements.txt`。上传照片的人脸检测和裁剪用到 `numpy` 和 `opencv-python-headless`，更新代码后需要安装：

```bash
uv pip install --python .venv/bin/python -r requirements.txt
```

HTTP 契约见 `../docs/API.md`。本轮实现项目、上传、样片、按风格拍组及任务控制；
模拟生成器原样返回第一张
参考照片，不会生成新的摄影作品。测试可以通过
`create_app(config=None, generator=None)` 注入生成器。

新建样片和拍组任务使用的提示词由 `../docs/prompts.md` 统一规范；已创建任务保留创建时的提示词快照。

运行时日志同时输出到控制台和 `<VOWFRAME_DATA_DIR>/logs/vowframe.log`，文件按
5 MB 轮转并保留 3 个备份。中断任务的 `error_code` 用于区分原因：
`interrupted_by_restart` 表示后端重启时请求仍在进行，`unknown_outcome` 表示生成请求
发出后连接中断，`download_failed` 表示图片已经生成但结果图片下载失败；三者都不会自动重发。

批量拍摄使用 `POST /api/projects/{id}/photo-batches`，请求必须引用已确认的导演草稿；
每张照片拥有独立的作品位置和任务，固定旧动作只用于展示和重组已有作品。
`/api/settings` 的 `batch_reference_mode`（`prompt_only` 或 `with_preview`）保存在
SQLite 的 `app_settings` 键值表中，并会影响之后新建的拍组任务。

### AI 导演运行细节

文字模型请求使用 60 秒超时和 `temperature = 0.9`。响应允许带 Markdown 代码围栏或 JSON
对象前后的说明文字，解析时提取首个 `{` 到最后一个 `}` 之间的 JSON；无效响应会触发一次
带校验问题的重写。导演请求的传输、鉴权和服务错误写入常规运行日志，日志位置为
`<VOWFRAME_DATA_DIR>/logs/vowframe.log`，密钥会脱敏。

## 配置与运行

| 环境变量 | 默认值 |
| --- | --- |
| `VOWFRAME_DATA_DIR` | 仓库的 `data/` 目录 |
| `VOWFRAME_MAX_CONCURRENCY` | `2` |
| `VOWFRAME_GENERATOR` | 覆盖本地文件中的 `generator`；文件缺失时为 `mock` |
| `VOWFRAME_MOCK_DELAY` | `2` 秒 |
| `VOWFRAME_LOCAL_CONFIG` | 仓库的 `backend/config.local.json`；可指定其他配置文件路径 |

### 启用 88API 图片生成

从仓库根目录创建本地配置：

```bash
cp backend/config.local.example.json backend/config.local.json
```

在本地编辑 `backend/config.local.json`，将 `api_key` 替换成自己的 88API 密钥。
该文件已加入 `.gitignore`，请勿提交、打印或分享其中的密钥。不要复用或读取
图片生成 skill 的私有配置文件。配置示例：

```json
{
  "generator": "88api",
  "api_key": "sk-...",
  "base_url": "https://api.88api.ai",
  "model": "gpt-image-2",
  "timeout": 600,
  "aspect_ratio": "3:4",
  "resolution": "2K"
}
```

没有本地文件时使用模拟生成器；设置 `VOWFRAME_GENERATOR=mock` 可以临时覆盖
文件中的真实生成器选择。配置在启动时读取，修改后需要重启。选择 `88api` 时，
缺少密钥或无效的图片参数会直接阻止启动，避免任务排队后才失败。`model` 仅用于
首次初始化默认模型；若配置中的模型不在内置列表中，服务会记录警告并使用
`gpt-image-2`，不会因该字段导致启动失败。

服务只接受 `/api/models` 返回的四个内置模型，并按注册表中的模型族选择请求格式：

- `gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`：OpenAI 请求格式。
- `gemini-nano-banana-2.1`：Gemini 请求格式。

其他模型 ID 均不接受。默认模型通过 `/api/settings` 修改并保存在 SQLite，立即对新任务生效，
无需重启。任务可在创建样片时通过 `model` 指定模型；省略时使用当前默认值。所有请求只生成
一张图片；参考图保持新娘在前、新郎在后的顺序。
OpenAI 系列使用 `/v1/images/edits` 的 `image[]` multipart 文件字段，根据比例和
分辨率映射像素尺寸，例如 `3:4` + `2K` 为 `1536x2048`。
Gemini 系列使用 `/v1/chat/completions`，内容依次为参考图 data URI、提示词，
图片设置放在 `extra_body.google.image_config`。

两类模型均支持 `1K`、`2K`、`4K`。由于请求比例需要对所有可选模型都有效，配置的
`aspect_ratio` 必须同时被 OpenAI 和 Gemini 支持；当前共有 `1:1`、`2:3`、`3:2`、
`3:4`、`4:3`、`9:16`、`16:9`、`21:9`。

**真实调用会产生费用。** 读取超时并不代表生成失败，服务端可能已经收到请求并扣费。
此时任务会中断，用户确认后才能重新生成；重新请求可能再次收费。返回图片链接后
下载失败也会中断，不能把下载失败当作生成请求未发送。密钥不会进入任务配置快照或
健康接口，服务商错误只进入脱敏日志，用户只看到中文提示。

测试强制使用模拟生成器；88API 测试通过 `httpx.MockTransport` 模拟所有请求，
不访问真实服务，不产生费用。

存在 `frontend/dist` 时，服务同时提供静态文件和单页应用的导航回退。
`/api` 路径始终返回后端接口结果或统一格式的中文错误。

使用单个服务进程启动。多个 Uvicorn 工作进程会分别执行启动恢复，并分别
限制并发数量，当前阶段没有实现多进程调度协调。

## 调度与保存

SQLite 包含 PRD 第 11 章的全部八类实体，每次操作单独打开连接，启用 WAL
及外键。项目阶段和批次状态均在读取时计算，不存入数据库。

默认模型和批量参考模式保存在独立的 `app_settings` 键值表中，初始化旧数据库时以
`INSERT OR IGNORE` 写入，不覆盖已保存的设置；旧数据库的批量参考模式只迁移一次。
每个新任务的模型写在
`model_config_snapshot`；重试复制原任务快照，所以默认模型变更不会改变已创建
任务或重试任务使用的模型。历史任务若快照没有 `model`，接口中的 `Task.model`
与 `VersionRef.model` 返回 `null`；调度器执行这类任务时使用当时的默认模型。

调度器使用事务领取等待任务，领取成功后才调用生成器。接口创建或继续任务
会立即唤醒调度器；轮询只用于补充发现数据库中的任务。生成器及文件写入通过
`asyncio.to_thread` 执行。图片先写入临时目录、刷新、同步到磁盘，再原子移动
到正式目录；随后在一个事务内保存图片记录和新版本、更新当前位置并完成任务。

只有确定请求尚未发出的 `not_sent` 错误会自动重试，最多两次。结果不明的
请求保持 `interrupted`，必须由用户主动重试。鉴权、余额或结果保存失败时，
同批等待任务转为 `held`；限流触发全局退避。手动重试创建新任务，保留原记录。
正常关闭会等待已提交请求完成。启动时，遗留运行任务转为 `interrupted`，
等待任务转为 `held`，并清理临时目录残留文件。

使用 `uvicorn --reload` 时，每次代码变更都会重启应用。若重启留下运行中的任务，
新进程会将它们标为 `interrupted`，不会自动重发；等待中的任务转为 `held`。
正常关闭若已等到调用完成，则该任务保持完成。真实付费生成期间应避免使用
`--reload`，以免频繁重启导致结果无法确认。

### 88API 错误分类

以下分类针对生成请求；图片下载发生在生成成功之后，其网络或 HTTP 失败均归为
`unknown_outcome`，防止下载错误触发新一轮付费生成。

| 条件 | 结果类型 | 处理 |
| --- | --- | --- |
| `ConnectError`（含 DNS 失败）、`ConnectTimeout` | `not_sent` | 确定未发送，可自动重试，最多两次 |
| `ReadTimeout`、`ReadError`、`WriteError`、`WriteTimeout`、协议错误及其他请求传输错误 | `unknown_outcome` | 标为 `interrupted`，不自动重发 |
| HTTP 401/403/402，或错误内容含 quota / balance / insufficient / 余额 / 配额 / 额度 / 欠费 | `auth_or_quota` | 当前任务失败，同批等待任务暂停，检查配置后继续剩余 |
| HTTP 429 | `rate_limited` | 全局退避；读取 `Retry-After` 秒数或 HTTP 日期，无有效值时使用现有退避规则 |
| HTTP 400/422，含审核拒绝 | `rejected` | 中文提示更换照片或稍后重试，不自动重发 |
| HTTP 502/504 | `unknown_outcome` | 中转服务失联，上游可能仍在生成并扣费；标为 `interrupted`，提示结果无法确认及重复生成可能额外扣费，不自动重发 |
| 其他 HTTP 5xx（如 500/503） | `rejected` | 中文提示模型服务暂时异常，不自动重发 |
| HTTP 200 但没有图片、JSON 无效或图片编码无效 | `rejected` | 中文提示模型没有返回图片，不自动重发 |
| 其他非成功 HTTP 状态 | `rejected` | 中文提示请求审核或参数无效，不自动重发 |

分类优先级：401/402/403 → 429 → 余额/配额错误内容 → 502/504 → 其他 HTTP 错误。
HTTP 200 中显式返回 `error` 且含余额/配额信息时，同样按 `auth_or_quota` 处理；
其他显式 `error` 按 `rejected` 处理。

## 验证环境说明

测试使用 `uvloop`，与 Linux 上 `uvicorn[standard]` 的默认事件循环一致。
提供的 Python 3.14 环境中，标准 asyncio 事件循环执行 AnyIO 跨线程唤醒时
出现停滞；使用 uvloop 可以完成测试，无需修改生产调度逻辑。

当前 Starlette 对已安装的 httpx 测试客户端发出弃用警告，测试仍可通过。
没有增加第三方依赖。执行环境禁止本机 TCP 连接，因此真实服务器能完成
启动与关闭，但本环境无法通过 TCP 请求健康接口；该限制不影响应用内接口测试。
