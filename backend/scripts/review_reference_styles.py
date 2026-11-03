"""Plan and render real reference-theme sets through the application's existing adapters.

Run from backend/: .venv/bin/python scripts/review_reference_styles.py plan|render|publish
Defaults to the nine themes and four shots each. Existing outputs are not requested again.
Review every picture before running publish, which copies only the nine catalogue samples.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api88 import Api88Generator  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.director import Api88Director, Moment, _shot_requirement, validate_moments  # noqa: E402
from app.generator import GeneratorError, OutcomeKind  # noqa: E402
from app.prompts import (STYLE_BY_ID, STYLE_OUTFITS, compose_moment_prompt, director_scene_count,  # noqa: E402
                         director_scene_text)
from app.reference_styles import REFERENCE_STYLE_IDS  # noqa: E402
from scripts.make_examples import (DEFAULT_STYLE_HAIR, PUBLIC, STYLE_HAIR, example_config, hair_references,  # noqa: E402
                                   make_styles, run, save_raw)

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / 'data' / 'style-review' / 'v720'
REFERENCE_IMAGES = dict(zip(REFERENCE_STYLE_IDS, (
    '2c5969b59eb3e1a72b875a45105150d8.jpg', '2ff2c5a5df0fc529ca905cd56ad0f2d1.jpg',
    '3cca0605c884752fa4cf8902c08cee49.jpg', '5a1928dc061fa4d64cf26935c8a9a13b.jpg',
    '37facae60fdacf3fc6081107f82408a3.jpg', '272df287c3825e7417dcbb9f6e02e483.jpg',
    '2977ceb20dfefae08457128d62c011ab.jpg', 'd1a065aa27b6edf946388cb0dd745994.jpg',
    'b82a174e117cab7c4d1db8636db9d024.jpg',
), strict=True))


def plan_context(style_id: str, count: int, with_preview: bool) -> dict:
    style = STYLE_BY_ID[style_id]
    return {
        'style_id': style_id,
        'outfit': STYLE_OUTFITS[style_id][0],
        'count': count,
        'with_preview': with_preview,
        'scene_count': director_scene_count(style_id, single_variant=with_preview),
        'scene_text': director_scene_text(style_id, single_variant=with_preview, outfit=STYLE_OUTFITS[style_id][0]),
        'shot_requirement': _shot_requirement(count, atmospheric=style['atmospheric']),
    }


def load_plan(path: Path, expected: dict) -> dict:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload['context'] != expected:
        raise ValueError(f'{path.name}: 拍摄规格已经变化，请另存旧方案后重新写方案')
    problems = validate_moments([Moment(**item) for item in payload['moments']], count=expected['count'],
                               scene_count=expected['scene_count'])
    if problems:
        raise ValueError(problems)
    return payload


def render_picture(generator, references: list[Path], prompt: str, model: str, ratio: str, path: Path,
                   *, provider: str, resolution: str):
    attempt_path = path.with_suffix('.attempt.json')
    history = []
    if attempt_path.exists():
        previous = json.loads(attempt_path.read_text(encoding='utf-8'))
        if previous.get('status') != 'failed' or previous.get('kind') not in (
                OutcomeKind.AUTH_OR_QUOTA, OutcomeKind.NOT_SENT, OutcomeKind.REJECTED):
            raise ValueError(f'{path.parent.name}/{path.name}: 已有请求记录且结果不明，需先核查上次结果')
        history = previous.get('previous_outcomes', []) + [
            {key: previous[key] for key in ('status', 'kind', 'error_code') if key in previous}]
    path.parent.mkdir(parents=True, exist_ok=True)
    attempt = {'provider': provider, 'model': model, 'resolution': resolution,
               'references': [str(ref.relative_to(ROOT)) if ref.is_relative_to(ROOT) else str(ref)
                              for ref in references],
               'aspect_ratio': ratio, 'status': 'requested', 'previous_outcomes': history}
    attempt_path.write_text(json.dumps(attempt, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.prompt.txt').write_text(prompt, encoding='utf-8')
    try:
        content = generator.generate_image(references, prompt, model, ratio)
    except GeneratorError as error:
        attempt.update(status='failed', kind=error.kind.value, error_code=error.error_code)
        attempt_path.write_text(json.dumps(attempt, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    save_raw(content, path)
    attempt['status'] = 'completed'
    attempt_path.write_text(json.dumps(attempt, ensure_ascii=False, indent=2), encoding='utf-8')


def make_plans(config, db, style_ids: list[str], count: int, output: Path):
    provider, model = db.get_director_provider(), db.get_director_model()
    if not model:
        raise ValueError('请先在应用设置中选择 AI 导演模型')
    with_preview = db.get_batch_reference_mode() == 'with_preview'
    director = Api88Director(config, model=model, provider=provider)
    print(f'director={provider} model={model} reference_mode={"with_preview" if with_preview else "prompt_only"}', flush=True)
    jobs = {}
    for style_id in style_ids:
        path = output / 'director' / f'{style_id}.json'
        context = plan_context(style_id, count, with_preview)
        if path.exists():
            load_plan(path, context)
            continue

        def job(style_id=style_id, path=path, context=context):
            moments = director.write_moments(STYLE_BY_ID[style_id]['name'], context['scene_text'], count,
                                             context['shot_requirement'], [], scene_count=context['scene_count'])
            problems = validate_moments(moments, count=count, scene_count=context['scene_count'])
            if problems:
                raise ValueError(problems)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'provider': provider, 'model': model, 'context': context,
                                        'moments': [asdict(moment) for moment in moments]},
                                       ensure_ascii=False, indent=2), encoding='utf-8')
        jobs[f'plan {style_id}'] = job
    return run(jobs)


def render_sets(config, db, style_ids: list[str], count: int, output: Path):
    selected, provider = example_config(config)
    generator = Api88Generator(selected, provider=provider)
    with_preview = db.get_batch_reference_mode() == 'with_preview'
    plans = {sid: load_plan(output / 'director' / f'{sid}.json', plan_context(sid, count, with_preview))
             for sid in style_ids}
    print(f'images={provider} model={selected.model} resolution={selected.resolution}', flush=True)
    failures = make_styles(selected, generator, style_ids=set(style_ids), output_dir=output / 'candidate')
    if failures:
        return failures
    jobs = {}
    for style_id, payload in plans.items():
        references = hair_references(STYLE_HAIR.get(style_id, DEFAULT_STYLE_HAIR))
        if with_preview:
            references.append(output / 'candidate' / 'styles' / f'{style_id}.webp')
        for index, moment in enumerate(payload['moments'], 1):
            path = output / 'sets' / style_id / f'{index:02}.jpg'
            ratio = '3:4' if moment['orientation'] == 'portrait' else '4:3'
            prompt = compose_moment_prompt(style_id, moment['text'], ratio, scene=moment['scene'],
                                           faces_clear=moment['faces_clear'], with_preview=with_preview,
                                           styled=True, outfit=payload['context']['outfit'])
            prompt_path = path.with_suffix('.prompt.txt')
            if path.exists():
                if not prompt_path.exists() or prompt_path.read_text(encoding='utf-8') != prompt:
                    raise ValueError(f'{style_id}/{index}: 已有图片与当前方案不一致，不能静默覆盖')
                continue

            def job(path=path, prompt=prompt, references=references, ratio=ratio):
                render_picture(generator, references, prompt, selected.model, ratio, path,
                               provider=provider, resolution=selected.resolution)
            jobs[f'set {style_id}/{index:02}'] = job
    return run(jobs)


def publish_samples(style_ids: list[str], count: int, output: Path, *, samples_only: bool = False):
    sources = []
    for style_id in style_ids:
        sample = output / 'candidate' / 'styles' / f'{style_id}.webp'
        if not sample.is_file() or (not samples_only and any(
                not (output / 'sets' / style_id / f'{i:02}.jpg').is_file() for i in range(1, count + 1))):
            raise ValueError(f'{style_id}: 样片或组照尚未完成，不能发布')
        sources.append(sample)
    destination = PUBLIC / 'styles'
    destination.mkdir(parents=True, exist_ok=True)
    for source in sources:
        shutil.copy2(source, destination / source.name)
    print(f'Published {len(sources)} reviewed catalogue samples.', flush=True)


def contact_sheets(style_ids: list[str], count: int, output: Path):
    """Keep the complete frame visible when comparing references and actual generated pictures."""
    font = ImageFont.load_default(size=19)

    def put(sheet, path, box):
        x, y, width, height = box
        with Image.open(path) as picture:
            thumb = ImageOps.contain(picture.convert('RGB'), (width, height))
        sheet.paste(thumb, (x + (width - thumb.width) // 2, y + (height - thumb.height) // 2))

    columns, cell_w, cell_h = 3, 400, 570
    samples = Image.new('RGB', (columns * cell_w, ((len(style_ids) + columns - 1) // columns) * cell_h), '#efefed')
    draw = ImageDraw.Draw(samples)
    for index, sid in enumerate(style_ids):
        x, y = index % columns * cell_w, index // columns * cell_h
        draw.text((x + 6, y + 8), f'{index + 1}. {sid}', font=font, fill='#222222')
        put(samples, output / 'candidate' / 'styles' / f'{sid}.webp', (x + 4, y + 36, cell_w - 8, cell_h - 42))
    samples.save(output / 'samples.jpg', quality=94)
    for sid in style_ids:
        rows = (count + 1) // 2
        sheet = Image.new('RGB', (1500, max(900, rows * 450 + 40)), '#efefed')
        draw = ImageDraw.Draw(sheet)
        draw.text((10, 10), 'REFERENCE', font=font, fill='#222222')
        reference = ROOT / REFERENCE_IMAGES[sid]
        if reference.is_file():
            put(sheet, reference, (5, 42, 590, 850))
        draw.text((610, 10), sid + ' / ACTUAL GENERATED SET', font=font, fill='#222222')
        for index in range(count):
            x, y = 604 + index % 2 * 448, 42 + index // 2 * 450
            put(sheet, output / 'sets' / sid / f'{index + 1:02}.jpg', (x, y, 440, 440))
        path = output / 'comparisons' / f'{sid}.jpg'
        path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(path, quality=93)
    print(f'Built samples sheet and {len(style_ids)} reference/set comparisons.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'render', 'contact', 'publish'))
    parser.add_argument('--style', action='append', choices=REFERENCE_STYLE_IDS)
    parser.add_argument('--count', type=int, choices=(4, 8, 12, 20), default=4)
    parser.add_argument('--output-dir', type=Path, default=REVIEW)
    parser.add_argument('--samples-only', action='store_true',
                        help='Publish reviewed samples while set review remains incomplete; applies only to publish.')
    args = parser.parse_args()
    if args.samples_only and args.action != 'publish':
        parser.error('--samples-only applies only to publish.')
    styles = args.style or list(REFERENCE_STYLE_IDS)
    if args.action == 'contact':
        contact_sheets(styles, args.count, args.output_dir)
        return
    if args.action == 'publish':
        publish_samples(styles, args.count, args.output_dir, samples_only=args.samples_only)
        return
    config = load_config()
    if not config.db_path.is_file():
        raise ValueError('请先初始化应用并选择服务；不会为审阅图另选付费服务')
    db = Database(config.db_path)
    failures = (make_plans(config, db, styles, args.count, args.output_dir) if args.action == 'plan'
                else render_sets(config, db, styles, args.count, args.output_dir))
    print('failed:', failures or 'none', flush=True)
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
