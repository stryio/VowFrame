"""Probe whether 88API keeps long image requests alive with streaming.

Background: non-streaming /v1/images/edits calls were dropped by 88API's nginx
gateway after 61-107 s ("Server disconnected without sending a response"), while a
2K image often takes longer. With `stream=true` the server should send events while
generating, so an idle-timeout gateway would not cut the connection.

Without --send this only prints what would be sent (no network, no cost).
With --send it makes exactly ONE paid generation request.

Usage (from backend/):
    .venv/bin/python scripts/probe_stream.py --project <project_id>            # dry run
    .venv/bin/python scripts/probe_stream.py --project <project_id> --send     # 1 paid call
Options: --model gpt-image-2.5-flare (default) | --no-stream (baseline) | --style <style_id>
"""

import argparse
import base64
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.models import MODELS_BY_ID  # noqa: E402
from app.prompts import STYLE_BY_ID, compose_preview_prompt  # noqa: E402

REPO = BACKEND.parent
CONFIG_PATH = BACKEND / "config.local.json"
DB_PATH = REPO / "data" / "vowframe.db"
OUT_DIR = REPO / "data" / "probe"


def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not config.get("api_key"):
        sys.exit("backend/config.local.json 中没有 api_key")
    return config


def reference_images(project_id: str) -> list[Path]:
    """Bride first, groom second — same order as real tasks."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)  # read-only
    try:
        row = conn.execute(
            "SELECT person_a_image_id, person_b_image_id, person_a_role FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            sys.exit(f"项目不存在：{project_id}")
        a, b, role_a = row
        if not a or not b:
            sys.exit("该项目还没有上传两张照片")
        ids = [a, b] if role_a == "bride" else [b, a]
        paths = []
        for asset_id in ids:
            (rel,) = conn.execute("SELECT file_path FROM image_assets WHERE id=?", (asset_id,)).fetchone()
            paths.append(REPO / "data" / rel)
        return paths
    finally:
        conn.close()


def openai_size(ratio: str, resolution: str) -> str:
    from app.api88 import OPENAI_SIZES

    return OPENAI_SIZES[ratio][resolution]


def redact(text: str, key: str) -> str:
    return text.replace(key, "[REDACTED]")


class Timeline:
    def __init__(self):
        self.start = time.monotonic()
        self.last = self.start

    def mark(self, label: str):
        now = time.monotonic()
        print(f"  +{now - self.start:7.1f}s  (距上次 {now - self.last:5.1f}s)  {label}", flush=True)
        self.last = now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--model", default="gpt-image-2.5-flare")
    parser.add_argument("--style", default="korean_minimal")
    parser.add_argument("--no-stream", action="store_true", help="baseline without streaming")
    parser.add_argument("--send", action="store_true", help="actually send ONE paid request")
    args = parser.parse_args()

    model = MODELS_BY_ID.get(args.model)
    if model is None or model.family != "openai":
        sys.exit("这个探测脚本只针对 OpenAI 系列模型（/v1/images/edits）")
    if args.style not in STYLE_BY_ID:
        sys.exit(f"未知风格：{args.style}")

    config = load_config()
    key = config["api_key"]
    base_url = config.get("base_url", "https://api.88api.ai").rstrip("/")
    ratio = config.get("aspect_ratio", "3:4")
    resolution = config.get("resolution", "2K")
    images = reference_images(args.project)
    prompt = compose_preview_prompt(args.style, ratio)
    stream = not args.no_stream

    fields = {"model": args.model, "prompt": prompt, "size": openai_size(ratio, resolution), "n": "1"}
    if stream:
        # OpenAI image streaming: SSE events; partial_images asks for progress frames,
        # which is exactly what keeps an idle-timeout gateway from cutting us off.
        fields["stream"] = "true"
        fields["partial_images"] = "2"

    print("请求概要")
    print(f"  POST {base_url}/v1/images/edits")
    print(f"  model={args.model} size={fields['size']} stream={stream}")
    print(f"  参考图（按顺序）：{[p.name for p in images]}")
    print(f"  提示词：{len(prompt)} 字（风格 {args.style}，V2）")
    if not args.send:
        print("\n演练模式：没有发送任何请求，没有产生费用。加上 --send 才会真实调用 1 次。")
        return

    files = [("image[]", (p.name, p.read_bytes(), "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png")) for p in images]
    timeline = Timeline()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outcome = "未完成"
    events = 0
    try:
        with httpx.Client(timeout=httpx.Timeout(600, connect=30), trust_env=False) as client:
            with client.stream(
                "POST",
                f"{base_url}/v1/images/edits",
                headers={"Authorization": f"Bearer {key}", "User-Agent": "vowframe-probe/1.0"},
                data=fields,
                files=files,
            ) as response:
                content_type = response.headers.get("content-type", "")
                timeline.mark(f"收到响应头 HTTP {response.status_code}，content-type={content_type}")
                if response.status_code != 200:
                    body = response.read().decode("utf-8", "replace")
                    print("  错误内容：", redact(body, key)[:800])
                    outcome = f"HTTP {response.status_code}"
                    return
                if "text/event-stream" not in content_type:
                    body = response.read()
                    timeline.mark(f"非流式响应，共 {len(body)} 字节（服务端忽略了 stream 参数）")
                    payload = json.loads(body)
                    b64 = (payload.get("data") or [{}])[0].get("b64_json")
                    if b64:
                        (OUT_DIR / "probe_nostream.png").write_bytes(base64.b64decode(b64))
                        outcome = "成功（非流式）"
                    else:
                        print("  响应：", redact(json.dumps(payload, ensure_ascii=False), key)[:800])
                        outcome = "无图片"
                    return

                event_type = None
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        events += 1
                        try:
                            payload = json.loads(data)
                        except ValueError:
                            timeline.mark(f"事件 {events}：非 JSON 数据 {redact(data, key)[:120]!r}")
                            continue
                        kind = payload.get("type") or event_type or "?"
                        b64 = payload.get("b64_json")
                        timeline.mark(f"事件 {events}：{kind}" + (f"，图片 {len(b64) * 3 // 4} 字节" if b64 else ""))
                        if b64:
                            name = "probe_final.png" if "completed" in kind else f"probe_partial_{events}.png"
                            (OUT_DIR / name).write_bytes(base64.b64decode(b64))
                            if "completed" in kind:
                                outcome = "成功（流式）"
                        if payload.get("error"):
                            print("  错误：", redact(json.dumps(payload["error"], ensure_ascii=False), key)[:800])
                            outcome = "服务端返回错误"
    except httpx.HTTPError as exc:
        timeline.mark(f"连接异常：{type(exc).__name__}: {redact(str(exc), key)[:200]}")
        outcome = f"中断（{type(exc).__name__}）"
    finally:
        total = time.monotonic() - timeline.start
        print(f"\n结果：{outcome}；总耗时 {total:.1f}s；流式事件 {events} 个")
        print(f"图片保存在：{OUT_DIR}")


if __name__ == "__main__":
    main()
