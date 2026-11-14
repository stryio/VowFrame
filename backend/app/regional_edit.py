"""Explicit annotation inputs for edits of one immutable photo version.

V7.79 crop edit ('crop', the default): the GPT image edit redraws the whole picture even with a mask, so a marked
hand stayed as small as before (1% of the frame, too few pixels for fingers), the faces were redrawn and the picture
came back smaller. Now the marked area is cut out with its surroundings, enlarged to 1024x1024 for the edit, and only
the marked area of the result is laid back, feathered, into the untouched original at its own size. Tasks recorded
with 'reference', 'markup' or 'mask' keep their whole-picture edit.
"""

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageFilter, ImageOps

AnnotationKind = Literal['reference', 'markup', 'mask', 'crop']
MAX_ANNOTATION_BYTES = 50 * 1024 * 1024
MAX_EDIT_PIXELS = 16_777_216
MARKUP_MODELS = frozenset({
    'gpt-image-2.5', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst',
    'gpt-image-2.6', 'gpt-image-2.6-flare', 'gpt-image-2.6-sunburst',
})
# 2026-11-11: models that take only the 'crop' edit. It sends two plain pictures, so any model that takes several
# references can do it; 'markup' and 'mask' are OpenAI edit fields, and 'reference' redraws the whole picture.
# 2026-11-13: Seedream 5.5 on 火山方舟 too (app/ark.py takes several references; the 1:1 1K crop is within its size range).
CROP_ONLY_MODELS = frozenset({'gemini-nano-banana-2.1', 'doubao-seedream-5-5-261028'})
REGIONAL_EDIT_MODELS = MARKUP_MODELS | CROP_ONLY_MODELS
# Verified 2026-11-08: this relay rejects the multipart markup field before generation.
UNSUPPORTED_MARKUP_PROVIDERS = frozenset({'pptoken'})


@dataclass(frozen=True)
class RegionalEdit:
    annotation: Path
    kind: AnnotationKind


def oriented_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as source:
        width, height = source.size
        return (height, width) if source.getexif().get(274) in (5, 6, 7, 8) else (width, height)


def validate_annotation(content: bytes, size: tuple[int, int], kind: AnnotationKind) -> bytes:
    """Validate before billing, preserving alpha while stripping annotation metadata."""
    if kind not in ('reference', 'markup', 'mask', 'crop'):
        raise ValueError('请选择圈选标记或蒙版编辑')
    if not content or len(content) > MAX_ANNOTATION_BYTES:
        raise ValueError('标记图不能为空，且不能超过 50 MB')
    if size[0] * size[1] > MAX_EDIT_PIXELS:
        raise ValueError('原图超过局部编辑的像素限制（16777216 像素）')
    try:
        with Image.open(io.BytesIO(content)) as source:
            if source.format != 'PNG':
                raise ValueError('标记图必须是带透明通道的 PNG 图片')
            if source.size != size:
                raise ValueError('标记图必须与方向校正后的原图尺寸相同，请重新打开编辑器')
            if 'A' not in source.getbands() and 'transparency' not in source.info:
                raise ValueError('标记图必须包含透明通道，不能与原图合并')
            image = source.convert('RGBA')
            low, high = image.getchannel('A').getextrema()
            if low != 0 or high == 0:
                raise ValueError('请标记需要编辑的区域，并保留标记以外的区域')
            output = io.BytesIO()
            image.save(output, 'PNG')
            return output.getvalue()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('无法读取标记图，请重新绘制后重试') from exc


def regional_edit_prompt(instruction: str, kind: AnnotationKind) -> str:
    if kind == 'reference':
        location = ('第二张图是第一张图的带标记参考副本，红色圈线标出了需要修改的对象。'
                    '第二张图只用于定位；请在干净的第一张原图上完成修改，输出中不要保留或重画任何红色标记。')
    else:
        location = ('独立透明标记层中的红色圈线标出了需要修改的对象。红线只是编辑指示，不是原照片的内容；'
                    '输出中不要保留或重画任何标记。' if kind == 'markup' else
                    '蒙版中完全透明的部分是需要修改的区域，不透明的部分应保持原样。')
    return '\n\n'.join((
        '第一张图是需要编辑的原照片。' + location,
        '用户要求：' + instruction,
        '只完成标记区域内要求的修改；保持其他区域、人物身份、表情、妆发、服饰、构图和光线。'
        '自然衔接编辑区域的边缘，不添加文字、水印或拼图。',
    ))


def annotated_reference(source: Path, annotation: bytes) -> tuple[str, bytes, str]:
    """An explicit compatibility method: the clean original remains a separate first input."""
    with Image.open(source) as original, Image.open(io.BytesIO(annotation)) as overlay:
        image = Image.alpha_composite(ImageOps.exif_transpose(original).convert('RGBA'), overlay.convert('RGBA'))
    output = io.BytesIO()
    image.convert('RGB').save(output, 'PNG')
    return 'annotated-reference.png', output.getvalue(), 'image/png'


def png_result(content: bytes) -> bytes:
    """Keep the PNG contract even when a relay ignores output_format."""
    try:
        with Image.open(io.BytesIO(content)) as source:
            if source.width * source.height > MAX_EDIT_PIXELS:
                raise ValueError('局部编辑结果超过像素限制，原图已保留')
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.mode not in ('RGB', 'RGBA', 'L', 'LA'):
                image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
            output = io.BytesIO()
            image.save(output, 'PNG')
            return output.getvalue()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('模型服务未返回有效图片，原图已保留；本次请求可能已经计费') from exc


# Crop edit (V7.79). The crop is a square twice the marked area's longer side, so the marked area fills about half of
# it and the edit sees the wrists, sleeves or bouquet around it; never smaller than MIN_CROP_SIDE, so a tiny mark
# still has context and is not enlarged into mush.
CROP_EDIT_SIDE = 1024
CROP_CONTEXT = 2.0
MIN_CROP_SIDE = 320


@dataclass(frozen=True)
class CropEdit:
    box: tuple[int, int, int, int]
    clean: bytes
    marked: bytes


def _region(annotation: Image.Image) -> Image.Image:
    """The marked area as an 'L' mask: everything the user painted (crop marks are filled shapes)."""
    return annotation.getchannel('A').point(lambda value: 255 if value else 0)


def crop_box(bbox: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """A square around the marked area, moved (not shrunk) to stay inside the picture."""
    width, height = size
    x0, y0, x1, y1 = bbox
    side = min(width, height, max(MIN_CROP_SIDE, round(max(x1 - x0, y1 - y0) * CROP_CONTEXT)))
    left = min(max(0, round((x0 + x1 - side) / 2)), width - side)
    top = min(max(0, round((y0 + y1 - side) / 2)), height - side)
    return left, top, left + side, top + side


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


def crop_edit_inputs(source: Path, annotation: Path) -> CropEdit:
    """The enlarged clean crop, and the same crop with the marked area tinted red to say where to edit."""
    with Image.open(source) as original, Image.open(annotation) as overlay:
        picture = ImageOps.exif_transpose(original).convert('RGB')
        region = _region(overlay.convert('RGBA'))
    bbox = region.getbbox()
    if bbox is None or region.size != picture.size:
        raise ValueError('标记与原图不一致，请重新打开编辑器标记')
    box = crop_box(bbox, picture.size)
    clean = picture.crop(box).resize((CROP_EDIT_SIDE, CROP_EDIT_SIDE), Image.Resampling.LANCZOS)
    tint = Image.new('RGB', clean.size, (239, 52, 52))
    alpha = region.crop(box).resize(clean.size, Image.Resampling.BILINEAR).point(lambda value: value * 45 // 100)
    marked = Image.composite(tint, clean, alpha)
    return CropEdit(box, _png(clean), _png(marked))


def paste_crop_result(source: Path, annotation: Path, box: tuple[int, int, int, int], content: bytes) -> bytes:
    """Lay only the marked area of the edited crop back into the original, with a soft edge; the rest of the
    picture stays pixel for pixel, at its own size."""
    try:
        with Image.open(io.BytesIO(content)) as result:
            if result.width * result.height > MAX_EDIT_PIXELS:
                raise ValueError('局部编辑结果超过像素限制，原图已保留')
            edited = ImageOps.exif_transpose(result).convert('RGB')
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('模型服务未返回有效图片，原图已保留；本次请求可能已经计费') from exc
    with Image.open(source) as original, Image.open(annotation) as overlay:
        picture = ImageOps.exif_transpose(original).convert('RGB')
        region = _region(overlay.convert('RGBA')).crop(box)
    side = box[2] - box[0]
    edited = edited.resize((side, side), Image.Resampling.LANCZOS)
    # Grow the mark a little so the new fingers are not cut at its line, then soften the seam.
    grow = max(3, side // 40) | 1
    blend = region.filter(ImageFilter.MaxFilter(grow)).filter(ImageFilter.GaussianBlur(max(2, side / 80)))
    picture.paste(Image.composite(edited, picture.crop(box), blend), box[:2])
    return _png(picture)


def crop_edit_prompt(instruction: str) -> str:
    return '\n\n'.join((
        '第一张图是一张婚纱照的局部放大图。第二张图是同一画面的副本，红色半透明区域标出了需要修改的位置；'
        '第二张图只用于定位，请在干净的第一张图上完成修改，输出中不要出现任何红色标记。',
        '用户要求：' + instruction,
        '只修改红色区域里的内容；区域以外的内容、位置、颜色、光线和清晰度都与第一张图一致。'
        '输出与第一张图完全相同的取景：不缩放、不平移、不重新构图，每样东西都在原来的位置上，'
        '这样修好的区域才能放回原照片。不添加文字、水印或拼图。',
    ))
