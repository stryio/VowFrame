---
name: image-gen
description: 通过 88API 生成或编辑图片，支持 gpt-image / az-gpt-image / Gemini / Grok Imagine / Midjourney / Seedream 等模型，可选模型、比例、分辨率、质量和参考图。用户要求生成图片、出图、画图、改图、图生图、用某个图片模型时使用。
---

# 88API 图片生成（VowFrame 项目专用）

所有请求都通过 `scripts/gen_image.py` 发出。API Key 保存在本目录的 `config.json` 中（已在 `.gitignore` 里忽略）。

## 密钥安全规则

- **不要**读取、打印、复述或修改 `config.json` 里的 `api_key`，也不要把密钥写进命令行参数、代码、日志或提交记录。
- 如果 `config.json` 不存在或 key 无效，脚本会报错。这时请用户自己执行
  `cp .claude/skills/image-gen/config.example.json .claude/skills/image-gen/config.json`，然后让用户自己填写 key。

## 选择模型

1. 用户指定了模型时，用 `-m <模型名>`。
2. 用户没指定时，使用 `config.json` 里的 `default_model`（不传 `-m`）。
3. `--list-models` 列出配置中的模型和说明。用户想改默认模型时，只修改 `config.json` 里的 `default_model` 字段。

| 需求 | 推荐模型 |
|---|---|
| 通用、写实、按张计费 | `gpt-image-2` / `gpt-image-2.5-flare` |
| 透明背景、一次多张、指定输出格式 | `az-gpt-image-2` 或 `az-gpt-image-2.5-*` |
| 多参考图（≤14）、人物一致性 | `gemini-3-pro-image` / `gemini-3.1-flash-image` |
| 快速、便宜、多比例 | `grok-imagine-image` |
| 艺术风格、一次 4 张 | `midjourney-v8.2`（`-fast` / `-turbo`） |
| 国风插画、一次最多 4 张 | `doubao-seedream-5-0-pro` |

## 用法

在项目根目录运行：

```bash
python3 .claude/skills/image-gen/scripts/gen_image.py "<提示词>" [选项]
```

| 选项 | 说明 |
|---|---|
| `-m MODEL` | 模型名 |
| `-a RATIO` | 比例，如 `1:1` `16:9` `3:4`（各模型支持的比例不同，脚本会校验） |
| `-R 1K\|2K\|4K` | 分辨率档位（Grok、Seedream 不支持 4K） |
| `-s WxH` | 仅 OpenAI 系列：直接指定像素尺寸 |
| `-q QUALITY` | gpt-image: low/medium/high；az-gpt-image-2.5: 另有 xhigh/max/auto；grok-2.0: low/medium/auto |
| `-n N` | 张数（az-gpt-image ≤10，grok ≤10，seedream ≤4） |
| `-r REF` | 参考图（本地路径 / URL / Data URI），可重复，按顺序对应提示词里的「第一张」「第二张」 |
| `--background transparent` | 仅 az-gpt-image |
| `--output-format png\|webp\|jpeg` | 仅 az-gpt-image |
| `-o DIR` | 输出目录，默认取 `config.json` 的 `output_dir` |

脚本会自动选择对应接口（images/generations、images/edits、chat/completions），完成后输出 JSON，`files` 是保存的图片路径。

## 示例

```bash
# 默认模型文生图
python3 .claude/skills/image-gen/scripts/gen_image.py "婚礼现场的花艺拱门，柔和自然光，写实摄影" -a 16:9 -R 2K -q medium

# Gemini 带参考图
python3 .claude/skills/image-gen/scripts/gen_image.py "参考第一张图的新人，生成同一对新人的海边婚纱照" -m gemini-3-pro-image -r photos/couple.jpg -a 2:3 -R 2K

# 透明背景贴纸
python3 .claude/skills/image-gen/scripts/gen_image.py "金色婚戒图标，扁平风格" -m az-gpt-image-2 --background transparent --output-format png

# Midjourney（旗标写在提示词里）
python3 .claude/skills/image-gen/scripts/gen_image.py "一座被雾气环绕的东方古城，电影感光影 --s 250" -m midjourney-v8.2 -a 16:9
```

## 注意

- Midjourney 不支持参考图；Seedream 参考图要用公网 URL（本地图片每次只能 1 张）；Grok 参考图最多 3 张；gpt-image-2 最多 4 张。
- 生成结束后告诉用户保存路径；需要时可以用 Read 工具查看图片。
- 失败请求（如内容审核未通过）一般不扣费，按报错信息修改提示词后重试。
