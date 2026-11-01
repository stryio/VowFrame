"""Example pictures for the hairstyle and style pickers, made once and shipped in frontend/public/examples.

One fictional couple is generated from text first (scripts/example_models/), then every hairstyle is edited onto
them with the app's own look prompt, and every style in use gets a sample shot from those looks with the app's own
sample prompt, so an example shows what the prompt asks for. Paid: about 40 image requests through 88API with the
model in config.local.json, at 1K.

Run from backend/:
    .venv/bin/python scripts/make_examples.py [models|hair|styles|all]

Existing pictures are kept; delete one to make it again (e.g. after its hairstyle or style text changed).
"""

from __future__ import annotations

import base64
import io
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api88 import OPENAI_SIZES, Api88Generator  # noqa: E402
from app.config import load_config  # noqa: E402
from app.prompts import (HAIRSTYLES, RETIRED_STYLE_IDS, STYLES, STYLING_ASPECT_RATIO, UPDO_STYLES,  # noqa: E402
                         compose_look_prompt, compose_preview_prompt)

BACKEND = Path(__file__).resolve().parents[1]
MODELS = BACKEND / 'scripts' / 'example_models'
# Full-size edits, the references of the style samples; kept out of git with the rest of data/.
RAW = BACKEND.parent / 'data' / 'examples_raw'
PUBLIC = BACKEND.parent / 'frontend' / 'public' / 'examples'
WIDTH = 512
WORKERS = 4

MODEL_PROMPTS = {
    'bride': ('写实的头肩人像摄影：一位二十五岁左右的中国女性，五官精致漂亮，眼睛明亮有神，鼻梁秀气，唇形饱满，'
              '气质温柔大方，皮肤白皙透亮、纹理真实细腻；黑色长发自然披在肩后，中分，发长过肩，额头和耳朵露出；'
              '穿简洁的白色细吊带上衣，露出锁骨和肩颈；正脸看向镜头，嘴角带一点自然的微笑；'
              '浅灰色纯色背景，柔和均匀的正面光，脸上没有明显的阴影；真实相机拍摄的照片质感，不是插画，不是网红脸。'),
    'groom': ('写实的头肩人像摄影：一位二十七岁左右的中国男性，五官端正英俊，眉眼清朗，下颌线清楚，'
              '气质干净斯文，皮肤干净、纹理真实；黑色短发，两侧修短，头顶自然蓬松；'
              '穿白色衬衫，领口敞开一粒扣子；正脸看向镜头，表情自然放松，带一点微笑；'
              '浅灰色纯色背景，柔和均匀的正面光，脸上没有明显的阴影；真实相机拍摄的照片质感，不是插画，不是网红脸。'),
}
# The bride's hairstyle in each style's sample: an updo where the style needs one.
STYLE_HAIR = {'chinese_traditional': 'chinese_bun', 'new_chinese': 'chinese_bun', 'hanfu': 'chinese_bun',
              'republic_qipao': 'finger_waves'}
DEFAULT_STYLE_HAIR = 'soft_waves'
# The bride's hairstyle examples wear one neutral makeup: the first style's.
HAIR_MAKEUP_STYLE = 'korean_minimal'


def text_to_image(config, prompt: str) -> bytes:
    """One picture from text alone (the app only edits, so this goes to the generations endpoint)."""
    response = httpx.post(
        config.base_url.rstrip('/') + '/v1/images/generations',
        headers={'Authorization': f'Bearer {config.api_key}'},
        json={'model': config.model, 'prompt': prompt, 'size': OPENAI_SIZES['3:4'][config.resolution], 'n': 1},
        timeout=config.timeout,
    )
    if not response.is_success:
        raise RuntimeError(f'generation failed: HTTP {response.status_code} {response.text[:300]}')
    item = response.json()['data'][0]
    if item.get('b64_json'):
        return base64.b64decode(item['b64_json'])
    return httpx.get(item['url'], timeout=config.timeout, follow_redirects=True).content


def save_example(content: bytes, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(io.BytesIO(content)).convert('RGB')
    image = image.resize((WIDTH, round(image.height * WIDTH / image.width)), Image.Resampling.LANCZOS)
    image.save(path, 'WEBP', quality=82, method=6)


def save_raw(content: bytes, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.open(io.BytesIO(content)).convert('RGB').save(path, 'JPEG', quality=92)


def run(jobs: dict[str, callable]):
    """Run the jobs (name -> callable) in parallel; a failure is reported and the rest go on."""
    failed = []
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(job): name for name, job in jobs.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
                print('done', name, flush=True)
            except Exception as error:  # report and go on: each picture is paid on its own
                failed.append(name)
                print('FAILED', name, repr(error)[:300], flush=True)
    return failed


def make_models(config):
    jobs = {}
    for role, prompt in MODEL_PROMPTS.items():
        path = MODELS / f'{role}.jpg'
        if not path.exists():
            jobs[f'model {role}'] = lambda path=path, prompt=prompt: save_raw(text_to_image(config, prompt), path)
    return run(jobs)


def make_hair(config, generator):
    jobs = {}
    for hairstyle in HAIRSTYLES:
        raw = RAW / 'hair' / f'{hairstyle.role}_{hairstyle.id}.jpg'
        public = PUBLIC / 'hair' / f'{hairstyle.role}_{hairstyle.id}.webp'
        if public.exists() and raw.exists():
            continue
        style_id = HAIR_MAKEUP_STYLE if hairstyle.role == 'bride' else None
        prompt = compose_look_prompt(hairstyle.role, hairstyle.id, 'natural', style_id=style_id)
        source = MODELS / f'{hairstyle.role}.jpg'

        def job(source=source, prompt=prompt, raw=raw, public=public):
            content = generator.generate_image([source], prompt, config.model, STYLING_ASPECT_RATIO)
            save_raw(content, raw)
            save_example(content, public)
        jobs[f'hair {hairstyle.role} {hairstyle.id}'] = job
    return run(jobs)


def make_styles(config, generator):
    jobs = {}
    for style in STYLES:
        if style['id'] in RETIRED_STYLE_IDS:
            continue
        public = PUBLIC / 'styles' / f"{style['id']}.webp"
        if public.exists():
            continue
        hair = STYLE_HAIR.get(style['id'], DEFAULT_STYLE_HAIR)
        assert style['id'] not in UPDO_STYLES or hair in STYLE_HAIR.values()
        references = [RAW / 'hair' / f'bride_{hair}.jpg', RAW / 'hair' / 'groom_keep.jpg']
        prompt = compose_preview_prompt(style['id'], '3:4', styled=True)

        def job(references=references, prompt=prompt, public=public):
            save_example(generator.generate_image(references, prompt, config.model, '3:4'), public)
        jobs[f"style {style['id']}"] = job
    return run(jobs)


def main():
    step = sys.argv[1] if len(sys.argv) > 1 else 'all'
    config = replace(load_config(), resolution='1K')
    generator = Api88Generator(config)
    failed = []
    if step in ('models', 'all'):
        failed += make_models(config)
    if step in ('hair', 'all') and not failed:
        failed += make_hair(config, generator)
    if step in ('styles', 'all') and not failed:
        failed += make_styles(config, generator)
    print('failed:', failed or 'none')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
