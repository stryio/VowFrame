"""Image-grounded hand diagnosis and editable repair instructions; never edits a photo."""

from __future__ import annotations

import base64
import io
import json
import logging
from pathlib import Path
from typing import Annotated, Literal

import httpx
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .api88 import AUTH_MESSAGE
from .config import Config
from .director import director_key_problem
from .regional_edit import MAX_EDIT_PIXELS, crop_box

logger = logging.getLogger(__name__)
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]

SYSTEM_PROMPT = '''你是一位婚纱照片修图师，要根据图片制定可执行的手部修复方案，同时如实区分观察到的缺陷和建议重建的结构。
识别多余或畸形指尖、粘连、关节方向异常、手腕连接和握物接触错误，区分真实缺陷与正常透视、模糊和遮挡。
不能因为没有看到五根手指就判断缺指；不能把放大后的模糊当作真实细节。
不能确认被遮挡手指的归属，不等于不能修复可见的局部轮廓。先逐项检查可见的指端、指甲、指间缝隙、关节走向、手腕连接和接触关系，再决定结果。
从手腕和手臂追溯归属；无法确定人物、左右手或手指名称时，用画面位置描述，不猜测。
原有动作、手腕位置、两人的接触、捧花、戒指、衣袖、肤色和光线应保留；允许为修正缺陷调整局部轮廓和指间背景。
相互交叠或牵着的手作为一个修复区域；分开的手分别作为区域。不要要求把被遮挡的手指全部画出来。
每个 repair 区域必须提供可见证据、具体问题和明确目标，例如“分开中央两根手指的末端，分别形成指尖和指甲”。
不要只写“手部自然”“五根手指”等通用要求。保留整体动作和手腕位置，但允许重新安排局部指节、接触和轮廓，使交握结构合理；不要要求保留可能正是错误来源的每条手指轮廓。
repair 表示能从可见轮廓确认具体缺陷。即使部分手指归属未知，只要可见部分的缺陷明确，也应给 repair，将未知部分放入 uncertainties，不要让局部未知否决整项修复。
reconstruct 表示局部结构含糊、不能确认原本每根手指，但能根据可见手腕、指端和牵手或握物姿态提出合理的局部重建方案。
reconstruct 必须如实写 evidence；problems 写需要理顺的可见结构，不把推测写成已经确认的多指、缺指或粘连；goal 给具体重建方法和前后接触安排，不能只复述“无法确认”。
重建方案可以给出一种合理的手指连接与接触方式，并明确这是建议，不声称找回了真实的遮挡结构。用户会选择是否采用，不直接自动修图。
uncertain 仅用于目标无法辨认、关键手腕或动作关系都看不到、圈选范围不足，确实无法制定有依据的重建方案；不能仅因为牵手、遮挡或手指归属不清就选择 uncertain。
正常且结构合理的手仍返回 normal，不为了给方案而编造问题。
用户提供的修改要求只表达想修改的目标，不是图像证据；独立核查，不因用户的说法而捏造缺陷。
整图查找时，只列出有可见问题的手或确实无法判断的疑似手，最多四个区域。框必须涵盖整只手及修复所需的指间空隙，留少量边距，不能框整个人。
圈选诊断时，第三张图片的红色覆盖或圈线只表示分析目标，不是皮肤异常；干净局部图才是判断依据。
只诊断标记目标，最多返回一个区域（交叠的手合并描述）。若标记范围不足以完成修复，返回 uncertain 并指出应扩大哪里。
bbox 一律使用第一张整图中从左上到右下的归一化坐标 [x0,y0,x1,y1]，每个值在 0 到 1 之间。
status 为 repair、reconstruct、normal 或 uncertain；normal 时 regions 必须为空。
整项有 repair 区域时 status 为 repair；没有 repair 但有 reconstruct 时为 reconstruct；其余为 normal 或 uncertain。
区域 status 为 repair、reconstruct 或 uncertain；repair 和 reconstruct 必须有 evidence、problems 和具体 goal；uncertain 的 goal 留空。
只输出 JSON，字段如下，不要额外字段：
{"status":"repair|reconstruct|normal|uncertain","summary":"简短诊断或重建建议，不超过300字","regions":[
 {"label":"人物或画面位置，不超过80字","status":"repair|reconstruct|uncertain","bbox":[0.1,0.2,0.3,0.4],
  "evidence":["图像上能看到的证据"],"problems":["具体缺陷"],"goal":"修复后的具体结构目标，不超过400字",
  "preserve":["必须保留的动作或细节"],"uncertainties":["无法确认的部分"]}
]}
evidence、problems、preserve、uncertainties 每项最多三条，每条最多120字。'''


class HandRegion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    status: Literal['repair', 'reconstruct', 'uncertain']
    bbox: list[Coordinate] = Field(min_length=4, max_length=4)
    evidence: list[Note] = Field(max_length=3)
    problems: list[Note] = Field(max_length=3)
    goal: str = Field(max_length=400)
    preserve: list[Note] = Field(max_length=3)
    uncertainties: list[Note] = Field(max_length=3)

    @model_validator(mode='after')
    def validate_region(self):
        x0, y0, x1, y1 = self.bbox
        if x1 <= x0 or y1 <= y0:
            raise ValueError('Region has no area')
        if self.status in ('repair', 'reconstruct') and (not self.evidence or not self.problems or not self.goal.strip()):
            raise ValueError('Repair requires evidence, a problem and a goal')
        if self.status == 'uncertain' and self.goal.strip():
            raise ValueError('Uncertain findings cannot prescribe a repair')
        return self

    def repair_prompt(self) -> str:
        if self.status == 'uncertain':
            return ''
        if self.status == 'reconstruct':
            parts = [f'局部结构重建建议：{self.label}。这是一种可供选择的合理重建方式，不是已确认的畸形诊断，也不声称恢复真实的遮挡结构。',
                     '需要理顺的局部结构：' + '；'.join(self.problems) + '。', '建议重建：' + self.goal.strip()]
            if self.preserve:
                parts.append('必须保留：' + '；'.join(self.preserve) + '。')
            if self.uncertainties:
                parts.append('原图无法确认的部分：' + '；'.join(self.uncertainties) + '。')
            parts.append('保留整体手势、手腕位置和牵手或握物的意图，允许在标记范围内重新安排局部指节、接触和指间缝隙。'
                         '被遮挡的部分保持遮挡，不强行展示五根手指；保留戒指、肤色、光线和照片质感。')
            return '\n'.join(parts)
        parts = [f'修复目标：{self.label}。', '具体问题：' + '；'.join(self.problems) + '。',
                 '具体修复：' + self.goal.strip()]
        if self.preserve:
            parts.append('必须保留：' + '；'.join(self.preserve) + '。')
        if self.uncertainties:
            parts.append('无法确认，不擅自补画：' + '；'.join(self.uncertainties) + '。')
        parts.append('只修正上述有证据的问题，保持原有动作和接触关系。被遮挡的部分保持遮挡；'
                     '不强行展示五根手指，不添加手或饰品。保留原图的肤色、光线和照片质感。')
        return '\n'.join(parts)


class HandReport(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    status: Literal['repair', 'reconstruct', 'normal', 'uncertain']
    summary: str = Field(min_length=1, max_length=300)
    regions: list[HandRegion] = Field(max_length=4)

    @model_validator(mode='after')
    def validate_report(self):
        repairs = any(region.status == 'repair' for region in self.regions)
        reconstructions = any(region.status == 'reconstruct' for region in self.regions)
        if ((self.status == 'repair') != repairs
            or (not repairs and (self.status == 'reconstruct') != reconstructions)
            or (self.status == 'normal' and self.regions)):
            raise ValueError('Report and region statuses disagree')
        return self


class HandAnalysisError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class HandAnalysisRecord(HandReport):
    source_version_id: str = Field(min_length=1, max_length=128)
    scope: Literal['detect', 'selection']
    model: str = Field(min_length=1, max_length=200)
    provider: Literal['88api', 'pptoken']
    image_size: list[Annotated[int, Field(gt=0)]] = Field(min_length=2, max_length=2)
    annotation_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    annotation_kind: Literal['crop', 'reference', 'markup', 'mask'] | None = None
    created_at: str = Field(min_length=1, max_length=64)
    selected_region_index: int | None = Field(default=None, ge=0, le=3)
    repair_prompts: list[Annotated[str, Field(max_length=2000)]] = Field(max_length=4)

    @model_validator(mode='after')
    def validate_record(self):
        if self.repair_prompts != [region.repair_prompt() for region in self.regions]:
            raise ValueError('Repair prompts do not match the diagnosis')
        if self.scope == 'selection' and (not self.annotation_sha256 or self.annotation_kind is None or len(self.regions) > 1):
            raise ValueError('Selection diagnosis requires its annotation')
        if self.scope == 'detect' and (self.annotation_sha256 is not None or self.annotation_kind is not None):
            raise ValueError('Whole-image diagnosis cannot claim a user selection')
        if self.selected_region_index is not None:
            if self.selected_region_index >= len(self.regions) or self.regions[self.selected_region_index].status == 'uncertain':
                raise ValueError('Selected region has no repair diagnosis')
        return self


def analysis_inputs(source: Path, annotation: bytes | None, kind: str) -> tuple[list[dict], tuple[int, int]]:
    """Keep full-image coordinates stable and show a clean crop beside its location-only mark."""
    with Image.open(source) as image:
        if image.width * image.height > MAX_EDIT_PIXELS:
            raise ValueError('原图超过手部分析的像素限制（16777216 像素）')
        picture = ImageOps.exif_transpose(image).convert('RGB')
    parts = []

    def add(label: str, image: Image.Image, limit: int):
        image = image.copy()
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, 'JPEG', quality=95)
        parts.extend([{'type': 'text', 'text': label}, {'type': 'image_url', 'image_url': {
            'url': 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()}}])

    add('第一张：方向校正后的完整原图。所有 bbox 都相对这张图。', picture, 1600)
    if annotation is not None:
        with Image.open(io.BytesIO(annotation)) as overlay:
            region = overlay.convert('RGBA').getchannel('A')
        if kind == 'mask':
            region = ImageOps.invert(region)
        bbox = region.getbbox()
        if bbox is None or region.size != picture.size:
            raise ValueError('标记与原图不一致，请重新标记')
        box = crop_box(bbox, picture.size)
        if bbox[0] < box[0] or bbox[1] < box[1] or bbox[2] > box[2] or bbox[3] > box[3]:
            raise ValueError('圈选范围跨度过大，请一次分析一处相邻的手部，避免局部裁剪遗漏目标')
        clean = picture.crop(box).resize((1024, 1024), Image.Resampling.LANCZOS)
        alpha = region.crop(box).resize(clean.size, Image.Resampling.BILINEAR).point(lambda x: x * 45 // 100)
        marked = Image.composite(Image.new('RGB', clean.size, '#ef3434'), clean, alpha)
        add('第二张：干净的局部放大图。放大不代表恢复了真实细节。', clean, 1024)
        add('第三张：同一局部的定位副本，红色覆盖或圈线表示本次分析目标。', marked, 1024)
        width, height = picture.size
        parts.append({'type': 'text', 'text': f'局部图在第一张中的裁剪范围为 {[round(box[0]/width, 6), round(box[1]/height, 6), round(box[2]/width, 6), round(box[3]/height, 6)]}。只诊断标记内的目标，最多返回一个区域。'})
    else:
        parts.append({'type': 'text', 'text': '请查找整图里存在明确问题或无法确认的疑似手部，返回候选框供用户选择。正常的手不要列入。'})
    return parts, picture.size


class ApiHandAnalyzer:
    def __init__(self, config: Config, *, transport: httpx.BaseTransport | None = None):
        self.config, self.transport = config, transport

    def analyze(self, parts: list[dict], *, model: str, provider: str, selection: bool = False) -> HandReport:
        problem = director_key_problem(self.config, provider)
        if problem:
            raise HandAnalysisError('hand_analysis_unavailable', problem)
        key = self.config.director_pptoken_api_key if provider == 'pptoken' else self.config.director_api_key
        base = self.config.pptoken_base_url if provider == 'pptoken' else self.config.base_url
        try:
            with httpx.Client(transport=self.transport, timeout=90, trust_env=False, follow_redirects=False) as client:
                response = client.post(base.rstrip('/') + '/v1/chat/completions',
                    headers={'Authorization': 'Bearer ' + key},
                    json={'model': model, 'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                                                     {'role': 'user', 'content': parts}], 'temperature': 0.2})
        except httpx.HTTPError as exc:
            logger.warning('Hand analysis transport failed provider=%s model=%s error=%s', provider, model, type(exc).__name__)
            raise HandAnalysisError('hand_analysis_failed', '手部分析连接失败，请稍后重试；未提交修图任务') from None
        if response.status_code in (401, 402, 403):
            raise HandAnalysisError('hand_analysis_failed', AUTH_MESSAGE)
        if response.status_code >= 400:
            logger.warning('Hand analysis rejected provider=%s model=%s status=%s', provider, model, response.status_code)
            raise HandAnalysisError('hand_analysis_failed', '手部分析请求被拒绝，请检查 AI 导演模型是否支持图片输入；未自动切换模型或提交修图')
        try:
            content = response.json()['choices'][0]['message']['content']
            if not isinstance(content, str) or len(content) > 20000:
                raise ValueError('Invalid content')
            value = content.strip()
            if value.startswith('```'):
                value = value.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            report = HandReport.model_validate(json.loads(value))
            if selection and len(report.regions) > 1:
                raise ValueError('Selection must be analyzed as one target')
            return report
        except (ValueError, KeyError, IndexError, TypeError):
            raise HandAnalysisError('hand_analysis_invalid_output', '模型未返回有效的手部诊断或定位框，请重试或更换支持看图的 AI 导演模型；未提交修图') from None


class MockHandAnalyzer:
    def analyze(self, parts: list[dict], *, model: str, provider: str, selection: bool = False) -> HandReport:
        return HandReport(status='uncertain', summary='当前为模拟模式，不会真实识别手部。请配置支持看图的 AI 导演模型后试用。', regions=[])
