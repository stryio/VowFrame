"""Example pictures for the hairstyle and style pickers, made once and shipped in frontend/public/examples.

One fictional couple is generated from text first (scripts/example_models/), then every hairstyle is edited onto
them with the app's own look prompt, and every style in use gets a sample shot from those looks with the app's own
sample prompt, so an example shows what the prompt asks for. Paid: each missing or explicitly regenerated picture
sends one image request through the provider and model selected in Settings, at 1K. Before the first app run,
the local configuration is used.

Run from backend/:
    .venv/bin/python scripts/make_examples.py [models|hair|styles|outfits|all] [--force] [--pptoken]
    .venv/bin/python scripts/make_examples.py styles --style korean_minimal --force --output-dir ../data/style-review/examples

Existing pictures are kept; --force makes the style and outfit pictures again (a failed one keeps the old picture).
--pptoken sends the requests to PPTOKEN with its gpt-image-2.5 instead.
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api88 import OPENAI_SIZES, Api88Generator  # noqa: E402
from app.config import Config, load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.generator import GeneratorError, OutcomeKind  # noqa: E402
from app.models import PROVIDERS, provider_for, require_model  # noqa: E402
from app.prompts import (HAIRSTYLES, RETIRED_STYLE_IDS, STYLE_OUTFITS, STYLES, STYLING_ASPECT_RATIO, UPDO_STYLES,  # noqa: E402
                         compose_look_prompt, compose_moment_prompt, compose_preview_prompt)

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
STYLE_HAIR = {'korean_minimal': 'low_bun', 'french_vintage': 'low_bun', 'forest_natural': 'half_up',
              'chinese_traditional': 'chinese_bun', 'new_chinese': 'chinese_bun', 'hanfu': 'chinese_bun',
              'republic_qipao': 'finger_waves',
              'european_heirloom': 'high_bun', 'french_couture': 'french_bun', 'minimal_bridal': 'low_bun',
              'white_horse_ceremony': 'half_up', 'blue_hour_veil': 'low_bun', 'forest_film': 'half_up',
              'retro_red_studio': 'low_bun', 'city_story': 'low_bun', 'chinese_celebration': 'chinese_bun'}
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
    """Bound concurrency and stop sending new requests after an authentication/quota failure."""
    failed, stopped = [], False
    remaining = iter(jobs.items())
    with ThreadPoolExecutor(WORKERS) as pool:
        pending = {}

        def submit_next():
            item = next(remaining, None)
            if item is not None:
                name, job = item
                pending[pool.submit(job)] = name

        for _ in range(WORKERS):
            submit_next()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                name = pending.pop(future)
                try:
                    future.result()
                    print('done', name, flush=True)
                except Exception as error:  # Report each failure; never retry a potentially paid request.
                    failed.append(name)
                    print('FAILED', name, repr(error)[:300], flush=True)
                    if isinstance(error, GeneratorError) and error.kind == OutcomeKind.AUTH_OR_QUOTA:
                        stopped = True
            if stopped:
                for name, _job in remaining:
                    failed.append(name)
                    print('NOT SENT', name, 'provider authentication or quota failure', flush=True)
            else:
                for _ in done:
                    submit_next()
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


def hair_references(hair: str) -> list[Path]:
    """The full-size edit when it is still here (data/ is not in git), else the shipped example."""
    result = []
    for name in (f'bride_{hair}', 'groom_keep'):
        raw = RAW / 'hair' / f'{name}.jpg'
        result.append(raw if raw.exists() else PUBLIC / 'hair' / f'{name}.webp')
    return result


# V7.17: a style with a choice of outfits shows each in full, so its neckline, skirt and train can be checked.
OUTFIT_SHOT = '50mm 镜头，平视机位，全身。两人并肩站在画面中央，脚下和裙摆完整入画，放松地牵着手，开心地看向镜头。'


def make_outfits(config, generator, *, style_ids: set[str] | None = None, force: bool = False,
                 output_dir: Path = PUBLIC):
    jobs = {}
    for style_id, outfits in STYLE_OUTFITS.items():
        if len(outfits) < 2 or (style_ids is not None and style_id not in style_ids):
            continue
        for outfit in outfits:
            public = output_dir / 'outfits' / f'{outfit}.webp'
            if public.exists() and not force:
                continue
            prompt = compose_moment_prompt(style_id, OUTFIT_SHOT, '3:4', scene=1, styled=True, outfit=outfit)
            references = hair_references(STYLE_HAIR.get(style_id, DEFAULT_STYLE_HAIR))

            def job(references=references, prompt=prompt, public=public, outfit=outfit):
                content = generator.generate_image(references, prompt, config.model, '3:4')
                raw = RAW / 'outfits' / f'{outfit}.jpg'
                save_raw(content, raw)
                raw.with_suffix('.prompt.txt').write_text(prompt, encoding='utf-8')
                save_example(content, public)
            jobs[f'outfit {outfit}'] = job
    return run(jobs)


def make_styles(config, generator, *, style_ids: set[str] | None = None, force: bool = False,
                output_dir: Path = PUBLIC):
    jobs = {}
    for style in STYLES:
        if style['id'] in RETIRED_STYLE_IDS or (style_ids is not None and style['id'] not in style_ids):
            continue
        public = output_dir / 'styles' / f"{style['id']}.webp"
        if public.exists() and not force:
            continue
        hair = STYLE_HAIR.get(style['id'], DEFAULT_STYLE_HAIR)
        assert style['id'] not in UPDO_STYLES or hair in STYLE_HAIR.values()
        references = hair_references(hair)
        prompt = compose_preview_prompt(style['id'], '3:4', styled=True, outfit=STYLE_OUTFITS[style['id']][0])

        def job(references=references, prompt=prompt, public=public, style_id=style['id']):
            content = generator.generate_image(references, prompt, config.model, '3:4')
            raw = RAW / 'styles' / f'{style_id}.jpg'
            save_raw(content, raw)
            raw.with_suffix('.prompt.txt').write_text(prompt, encoding='utf-8')
            save_example(content, public)
        jobs[f"style {style['id']}"] = job
    return run(jobs)


def example_config(config: Config, *, pptoken: bool = False) -> tuple[Config, str]:
    """Use the app's current provider/model without changing its settings or trying another paid service."""
    if pptoken:
        model, provider = 'gpt-image-2.5', 'pptoken'
    elif config.db_path.is_file():
        db = Database(config.db_path)
        model = db.get_default_model()
        provider = provider_for(require_model(model), db.get_image_provider())
    else:
        model = config.model
        provider = provider_for(require_model(model), '88api')
    config = replace(config, resolution='1K', model=model)
    if provider == 'pptoken':
        if not config.pptoken_api_key.strip():
            raise ValueError('PPTOKEN 没有配置密钥；不会改用其他图片服务')
        config = replace(config, base_url=config.pptoken_base_url, api_key=config.pptoken_api_key)
    return config, PROVIDERS[provider]


def main():
    parser = argparse.ArgumentParser(description='Generate examples through the application image provider at 1K.')
    parser.add_argument('step', nargs='?', default='all', choices=('models', 'hair', 'styles', 'outfits', 'all'))
    parser.add_argument('--force', action='store_true', help='Regenerate existing style and outfit examples.')
    parser.add_argument('--pptoken', action='store_true', help='Use PPTOKEN and its gpt-image-2.5 model.')
    parser.add_argument('--style', action='append', choices=tuple(STYLE_OUTFITS), help='Limit styles/outfits to this style; repeat to select several.')
    parser.add_argument('--output-dir', type=Path, default=PUBLIC, help='Destination for style/outfit thumbnails; hair references still use shipped examples.')
    args = parser.parse_args()
    if args.style and args.step in ('models', 'hair'):
        parser.error('--style applies to styles, outfits or all.')
    step = args.step
    style_ids = set(args.style) if args.style else None
    config, provider = example_config(load_config(), pptoken=args.pptoken)
    generator = Api88Generator(config, provider=provider)
    print(f'provider={provider} model={config.model} resolution={config.resolution} step={step}', flush=True)
    failed = []
    if step in ('models', 'all'):
        failed += make_models(config)
    if step in ('hair', 'all') and not failed:
        failed += make_hair(config, generator)
    if step in ('styles', 'all') and not failed:
        failed += make_styles(config, generator, style_ids=style_ids, force=args.force, output_dir=args.output_dir)
    if step in ('outfits', 'all') and not failed:
        failed += make_outfits(config, generator, style_ids=style_ids, force=args.force, output_dir=args.output_dir)
    print('failed:', failed or 'none')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
