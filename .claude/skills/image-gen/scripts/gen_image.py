#!/usr/bin/env python3
"""88API 图片生成脚本：统一调用 OpenAI / Gemini / Grok / Midjourney / Seedream 图片模型。

密钥从同目录上级的 config.json 读取（已被 .gitignore 忽略），绝不写在命令行或代码里。
仅依赖 Python 标准库。
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("IMAGE_GEN_CONFIG", SKILL_DIR / "config.json"))

# OpenAI 系列推荐尺寸：比例 -> {档位: 像素}
OPENAI_SIZES = {
    "1:1": {"1K": "1024x1024", "2K": "2048x2048", "4K": "2880x2880"},
    "3:2": {"1K": "1536x1024", "2K": "2400x1600", "4K": "3504x2336"},
    "2:3": {"1K": "1024x1536", "2K": "1600x2400", "4K": "2336x3504"},
    "4:3": {"1K": "1152x864", "2K": "2048x1536", "4K": "3264x2448"},
    "3:4": {"1K": "864x1152", "2K": "1536x2048", "4K": "2448x3264"},
    "16:9": {"1K": "1280x720", "2K": "2560x1440", "4K": "3840x2160"},
    "9:16": {"1K": "720x1280", "2K": "1440x2560", "4K": "2160x3840"},
    "21:9": {"1K": "1344x576", "2K": "3024x1296", "4K": "3840x1648"},
    "2:1": {"1K": "1408x704", "2K": "2880x1440", "4K": "3840x1920"},
    "1:2": {"1K": "704x1408", "2K": "1440x2880", "4K": "1920x3840"},
}
GEMINI_RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
GROK_RATIOS = {"auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2",
               "19.5:9", "9:19.5", "20:9", "9:20", "21:9", "5:2"}

# 参考图上限
REF_LIMITS = {
    "gpt-image-2": 4,
    "gemini-nano-banana-2.1": 6,
}


def die(msg):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(1)


def load_config():
    if not CONFIG_PATH.exists():
        die(f"未找到配置文件 {CONFIG_PATH}，请复制 config.example.json 为 config.json 并填入 api_key")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    key = cfg.get("api_key", "")
    if not key or key.startswith("sk-xxxx"):
        die(f"请在 {CONFIG_PATH} 中填写有效的 api_key")
    cfg.setdefault("base_url", "https://api.88api.ai")
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    cfg.setdefault("default_model", "gpt-image-2")
    cfg.setdefault("output_dir", "outputs/images")
    cfg.setdefault("timeout", 600)
    return cfg


def family(model):
    m = model.lower()
    if m.startswith("az-gpt-image"):
        return "az-openai"
    if m.startswith("gpt-image"):
        return "openai"
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith("grok-imagine"):
        return "grok"
    if m.startswith("midjourney"):
        return "midjourney"
    if m.startswith("doubao-seedream"):
        return "seedream"
    die(f"不认识的模型: {model}")


# ---------- HTTP ----------

def http(cfg, method, path, body=None, headers=None):
    url = path if path.startswith("http") else cfg["base_url"] + path
    h = {"User-Agent": "vowframe-image-gen/1.0"}
    if not path.startswith("http"):
        h["Authorization"] = f"Bearer {cfg['api_key']}"
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:2000]
        die(f"HTTP {e.code} {url}\n{detail}")
    except urllib.error.URLError as e:
        die(f"请求失败 {url}: {e.reason}")


def post_json(cfg, path, payload):
    raw, _ = http(cfg, "POST", path, json.dumps(payload).encode("utf-8"),
                  {"Content-Type": "application/json"})
    return json.loads(raw)


def post_multipart(cfg, path, fields, files):
    """fields: [(name, value)]；files: [(name, filename, bytes, mime)]"""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, data, mime in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    raw, _ = http(cfg, "POST", path, b"".join(parts),
                  {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.loads(raw)


# ---------- 参考图 ----------

def is_url(s):
    return s.startswith("http://") or s.startswith("https://")


def read_local(path):
    p = Path(path).expanduser()
    if not p.is_file():
        die(f"参考图不存在: {path}")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return p.name, p.read_bytes(), mime


def to_data_uri(ref):
    if ref.startswith("data:") or is_url(ref):
        return ref
    _, data, mime = read_local(ref)
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def to_file(cfg, ref, idx):
    """把参考图转成 multipart 文件元组（URL 会先下载）。"""
    if is_url(ref):
        data, ctype = http(cfg, "GET", ref)
        mime = ctype.split(";")[0] or "image/png"
        return f"ref{idx}{mimetypes.guess_extension(mime) or '.png'}", data, mime
    if ref.startswith("data:"):
        m = re.match(r"data:([^;]+);base64,(.*)", ref, re.S)
        if not m:
            die("Data URI 格式不正确")
        return f"ref{idx}{mimetypes.guess_extension(m.group(1)) or '.png'}", base64.b64decode(m.group(2)), m.group(1)
    return read_local(ref)


# ---------- 各模型族的请求构造 ----------

def openai_size(args):
    if args.size:
        if args.size == "auto":
            return "auto"
        m = re.fullmatch(r"(\d+)x(\d+)", args.size)
        if not m:
            die("--size 需写成 宽x高，例如 2048x2048")
        w, h = int(m.group(1)), int(m.group(2))
        if w % 16 or h % 16 or max(w, h) > 3840 or max(w, h) / min(w, h) > 3 \
                or not 655360 <= w * h <= 8294400:
            die("尺寸不合法：宽高须为 16 的倍数、最长边 ≤3840、长短边比 ≤3:1、总像素 655360~8294400")
        return args.size
    if args.aspect:
        if args.aspect not in OPENAI_SIZES:
            die(f"OpenAI 图片模型支持的比例: {', '.join(OPENAI_SIZES)}")
        return OPENAI_SIZES[args.aspect][args.resolution or "1K"]
    if args.resolution:
        return OPENAI_SIZES["1:1"][args.resolution]
    return None


def run_openai(cfg, args, fam):
    size = openai_size(args)
    extra = {}
    if fam == "az-openai":
        for k in ("background", "output_format", "n"):
            v = getattr(args, k)
            if v is not None:
                extra[k] = v
    elif args.n and args.n > 1:
        die("gpt-image-* 每次只返回 1 张，多张请用 az-gpt-image-* 或多次调用")
    elif args.background or args.output_format:
        die("--background / --output-format 仅 az-gpt-image-* 支持")

    if not args.ref:
        payload = {"model": args.model, "prompt": args.prompt, **extra}
        if size:
            payload["size"] = size
        if args.quality:
            payload["quality"] = args.quality
        return post_json(cfg, "/v1/images/generations", payload)

    fields = [("model", args.model), ("prompt", args.prompt)]
    if size:
        fields.append(("size", size))
    if args.quality:
        fields.append(("quality", args.quality))
    fields += [(k, str(v)) for k, v in extra.items()]
    files = [("image[]", *to_file(cfg, r, i)) for i, r in enumerate(args.ref)]
    return post_multipart(cfg, "/v1/images/edits", fields, files)


def run_gemini(cfg, args):
    if args.aspect and args.aspect not in GEMINI_RATIOS:
        die(f"Gemini 支持的比例: {', '.join(sorted(GEMINI_RATIOS))}")
    if args.ref:
        content = [{"type": "text", "text": args.prompt}] + [
            {"type": "image_url", "image_url": {"url": to_data_uri(r)}} for r in args.ref]
    else:
        content = args.prompt
    payload = {"model": args.model, "messages": [{"role": "user", "content": content}]}
    image_config = {}
    if args.aspect:
        image_config["aspect_ratio"] = args.aspect
    if args.resolution:
        image_config["image_size"] = args.resolution
    if image_config:
        payload["extra_body"] = {"google": {"image_config": image_config}}
    resp = post_json(cfg, "/v1/chat/completions", payload)
    text = resp["choices"][0]["message"].get("content") or ""
    data = []
    for uri in re.findall(r"\((data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+)\)", text):
        data.append({"data_uri": uri})
    for url in re.findall(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", text):
        data.append({"url": url})
    if not data:
        die(f"响应中没有图片：{text[:500]}")
    return {"data": data}


def run_grok(cfg, args):
    if args.aspect and args.aspect not in GROK_RATIOS:
        die(f"Grok 支持的比例: {', '.join(sorted(GROK_RATIOS))}")
    if args.resolution == "4K":
        die("Grok 只支持 1K / 2K")
    payload = {"model": args.model, "prompt": args.prompt}
    if args.aspect:
        payload["aspect_ratio"] = args.aspect
    if args.resolution:
        payload["resolution"] = args.resolution.lower()
    if args.n:
        payload["n"] = args.n
    if args.quality:
        payload["quality"] = args.quality
    if not args.ref:
        return post_json(cfg, "/v1/images/generations", payload)
    refs = [to_data_uri(r) for r in args.ref]
    if len(refs) == 1:
        payload["image"] = refs[0]
    else:
        payload["images"] = refs
    return post_json(cfg, "/v1/images/edits", payload)


def run_midjourney(cfg, args):
    if args.ref:
        die("Midjourney 不支持参考图")
    prompt = args.prompt
    if args.aspect and "--ar" not in prompt:
        prompt += f" --ar {args.aspect}"
    return post_json(cfg, "/v1/images/generations", {"model": args.model, "prompt": prompt})


def run_seedream(cfg, args):
    tier = args.resolution or "2K"
    if tier == "4K":
        die("Seedream 只支持 1K / 2K")
    base = {"model": args.model, "prompt": args.prompt, "size": tier, "quality": tier}
    if args.n:
        base["n"] = args.n
    refs = args.ref or []
    urls = [r for r in refs if is_url(r)]
    locals_ = [r for r in refs if not is_url(r)]
    if locals_ and (urls or len(locals_) > 1):
        die("Seedream 本地参考图每次只能传 1 张；多张请使用公网 URL")
    if locals_:
        fields = [(k, str(v)) for k, v in base.items()]
        return post_multipart(cfg, "/v1/images/edits", fields, [("image", *read_local(locals_[0]))])
    if len(urls) == 1:
        base["image"] = urls[0]
    elif urls:
        base["images"] = urls
    return post_json(cfg, "/v1/images/generations", base)


# ---------- 保存 ----------

def sniff_ext(data):
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


def save_all(cfg, resp, out_dir, model):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    saved = []
    for i, item in enumerate(resp.get("data") or []):
        if item.get("b64_json"):
            data = base64.b64decode(item["b64_json"])
        elif item.get("data_uri"):
            data = base64.b64decode(item["data_uri"].split(",", 1)[1])
        elif item.get("url"):
            data, _ = http(cfg, "GET", item["url"])
        else:
            continue
        path = out / f"{stamp}-{model}-{i + 1}{sniff_ext(data)}"
        path.write_bytes(data)
        saved.append(str(path))
    if not saved:
        die(f"响应中没有可保存的图片：{json.dumps(resp, ensure_ascii=False)[:500]}")
    return saved


def main():
    ap = argparse.ArgumentParser(description="88API 图片生成")
    ap.add_argument("prompt", nargs="?", help="图片描述 / 编辑指令")
    ap.add_argument("-m", "--model", help="模型名；不填用 config.json 的 default_model")
    ap.add_argument("-r", "--ref", action="append", help="参考图（本地路径 / URL / Data URI），可重复")
    ap.add_argument("-a", "--aspect", help="比例，如 1:1、16:9、3:4")
    ap.add_argument("-R", "--resolution", type=str.upper, choices=["1K", "2K", "4K"], help="分辨率档位")
    ap.add_argument("-s", "--size", help="OpenAI 系列可直接指定像素，如 2048x2048")
    ap.add_argument("-q", "--quality", help="质量档位（low/medium/high/xhigh/max/auto，视模型而定）")
    ap.add_argument("-n", type=int, help="张数（az-gpt-image / grok / seedream 支持）")
    ap.add_argument("--background", choices=["transparent", "opaque", "auto"], help="仅 az-gpt-image")
    ap.add_argument("--output-format", dest="output_format", choices=["png", "webp", "jpeg"], help="仅 az-gpt-image")
    ap.add_argument("-o", "--out", help="输出目录；不填用 config.json 的 output_dir")
    ap.add_argument("--list-models", action="store_true", help="列出 config.json 中配置的模型")
    args = ap.parse_args()

    cfg = load_config()
    if args.list_models:
        print(json.dumps({"default_model": cfg["default_model"], "models": cfg.get("models", {})},
                         ensure_ascii=False, indent=2))
        return
    if not args.prompt:
        die("缺少 prompt")

    args.model = args.model or cfg["default_model"]
    fam = family(args.model)
    limit = REF_LIMITS.get(args.model, {"grok": 3, "gemini": 14, "seedream": 14}.get(fam, 10))
    if args.ref and len(args.ref) > limit:
        die(f"{args.model} 最多 {limit} 张参考图")

    runner = {
        "openai": lambda: run_openai(cfg, args, fam),
        "az-openai": lambda: run_openai(cfg, args, fam),
        "gemini": lambda: run_gemini(cfg, args),
        "grok": lambda: run_grok(cfg, args),
        "midjourney": lambda: run_midjourney(cfg, args),
        "seedream": lambda: run_seedream(cfg, args),
    }[fam]
    t0 = time.time()
    resp = runner()
    saved = save_all(cfg, resp, args.out or cfg["output_dir"], args.model)
    print(json.dumps({"model": args.model, "seconds": round(time.time() - t0, 1), "files": saved},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
