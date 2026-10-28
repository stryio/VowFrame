"""AI director adapters and shared moment validation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Protocol

import httpx

from .api88 import AUTH_MESSAGE
from .aspect import ORIENTATION_NAMES, ORIENTATIONS
from .config import Config
from .prompts import DETAIL_SHOT

logger = logging.getLogger(__name__)

# The standard five-step shot ladder. Older works may still carry the retired studio labels
# three_quarter / half and faceless detail shots; they are read and shown but never written for new shots.
SHOT_SIZES = ('wide', 'full', 'medium', 'close', 'extreme_close')
SHOT_NAMES = {'wide': '远景', 'full': '全景', 'medium': '中景', 'close': '近景', 'extreme_close': '特写',
              DETAIL_SHOT: '局部特写', 'three_quarter': '七分身', 'half': '半身'}
SHOT_MEANINGS = ('wide 远景（人物很小，环境为主），full 全景（全身入画），medium 中景（膝盖或腰部以上），'
                 'close 近景（胸口以上），extreme_close 特写（只拍面部）')
FORBIDDEN_WORDS = (
    '眼睛', '眼型', '眼神清澈', '双眼皮', '单眼皮', '丹凤', '杏眼', '桃花眼', '睫毛', '眉毛', '眉形',
    '鼻子', '鼻梁', '鼻型', '嘴唇', '唇形', '红唇', '脸型', '瓜子脸', '鹅蛋脸', '圆脸', '尖下巴',
    '下颌', '颧骨', '酒窝', '雀斑', '皮肤', '肤色', '五官', '长相', '背对镜头', '背影', '剪影',
    '遮住脸', '挡住脸', '墨镜', '口罩',
)
MAX_TEXT_LENGTH = 220
SYSTEM_PROMPT = '''你是一位资深的婚纱摄影师兼导演，为一对新人设计照片。服装、视觉语言和可用的场景已由摄影团队确定，你要决定的是：每张照片在哪里拍、怎么拍。

每张照片写成一段「镜头」，依次交代：
1. 机位与焦段：机位的高低和角度，用什么焦段（例如 35mm 交代环境，85mm 压缩背景、浅景深）。
2. 构图：景别（远景、全景、中景、近景或特写），竖版还是横版，两人在画面里的位置和大小，留白在哪里，有没有前景。以竖版为主；交代环境的远景、需要横向延展的画面（例如海平面、天际线、长廊）适合横版。
3. 人与场景：使用哪个场景（有多个场景变体时选一个），写清背景、两人所在的位置和与哪个场景元素发生关系。
4. 光：这个场景的光从哪个方向照到两人身上，同一束光也照在周围的环境上；灯具等光源本身不要出现在画面里。
5. 瞬间：一个真实发生的小事件，写清动作、互动、表情和视线落点。

必须遵守：
1. 不描述任何五官或长相（例如眼睛、脸型、鼻子、嘴唇、皮肤），长相由参考照片决定。
2. 两人的面部清晰可见，不被手、头发、捧花、头纱或道具遮挡；不要背对镜头，不要剪影。
3. 不写服装；光只能来自所选场景里的光源。
4. 动作自然、真实可拍，避免多人、复杂道具和夸张特技。
5. 表情自然、真实，符合这个瞬间的情绪；整组的表情要有变化。
6. 各个镜头彼此不同，也不要与「已拍过的镜头」重复。
7. 只写具体的拍摄决定；整体画质与质感已写在发给生图模型的画面质感段里，镜头里不必重复。
8. 只输出 JSON，不要输出任何其他文字。

写法示例（只示意写法，不要照搬内容）：
50mm 镜头，略高于两人视线，中景。旧书房的窗边，午后的阳光从画面左侧的窗户斜射进来，照亮两人和身后的书架。两人位于画面左侧，右侧留出被阳光照亮的书架。新郎从身后轻轻环住新娘，在她耳边说了句悄悄话，她笑着侧过脸看他，新郎看向镜头。'''


@dataclass(frozen=True)
class Moment:
    title: str
    shot_size: str
    text: str
    orientation: str


class DirectorError(Exception):
    """Base class for director failures exposed by the API."""


class DirectorFailed(DirectorError):
    def __init__(self, reason: str = 'AI 导演暂时无法响应，请稍后重试'):
        self.reason = reason
        super().__init__(reason)


class DirectorInvalidOutput(DirectorError):
    def __init__(self, problems: list[str] | None = None):
        self.problems = problems or []
        super().__init__('AI 导演没有写出符合要求的方案，请重试')


class Director(Protocol):
    def write_moments(self, style: str, scene_text: str, count: int, shot_requirement: str,
                      avoid_titles: list[str]) -> list[Moment]: ...

    def rewrite_shot(self, style: str, scene_text: str, original_text: str, target_shot: str,
                     orientation: str) -> Moment: ...


def _cjk_or_basic_length(value: str) -> int:
    return len(value)


def validate_moment(moment: Moment | dict, *, avoid_titles: list[str] = (), target_shot: str | None = None,
                    target_orientation: str | None = None) -> list[str]:
    if isinstance(moment, dict):
        title, shot_size, text = moment.get('title'), moment.get('shot_size'), moment.get('text')
        orientation = moment.get('orientation')
    else:
        title, shot_size, text, orientation = moment.title, moment.shot_size, moment.text, moment.orientation
    problems: list[str] = []
    if not isinstance(title, str) or not (2 <= _cjk_or_basic_length(title) <= 12) or not re.search(r'[\u3400-\u9fff]', title or ''):
        problems.append('标题必须为 2～12 个字符且至少包含一个汉字')
    if not isinstance(shot_size, str) or shot_size not in SHOT_SIZES:
        problems.append('shot_size 必须是 ' + '、'.join(SHOT_SIZES) + ' 之一')
    if orientation not in ORIENTATIONS:
        problems.append('orientation 必须是 portrait 或 landscape')
    if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH or not text.endswith('。'):
        problems.append(f'text 必须不超过 {MAX_TEXT_LENGTH} 个字符并以“。”结尾')
    if isinstance(text, str) and any(word in text for word in FORBIDDEN_WORDS):
        problems.append('text 包含禁用词：' + '、'.join(word for word in FORBIDDEN_WORDS if word in text))
    if isinstance(title, str) and any(word in title for word in FORBIDDEN_WORDS):
        problems.append('title 包含禁用词')
    if isinstance(title, str) and title in set(avoid_titles):
        problems.append('title 与已拍过的瞬间重复')
    if target_shot is not None and shot_size != target_shot:
        problems.append(f'shot_size 必须为 {target_shot}')
    if target_orientation is not None and orientation != target_orientation:
        problems.append(f'orientation 必须为 {target_orientation}')
    return problems


def validate_moments(moments: list[Moment], *, count: int | None = None, avoid_titles: list[str] = ()) -> list[str]:
    problems: list[str] = []
    if count is not None and len(moments) != count:
        problems.append(f'需要 {count} 个瞬间，实际为 {len(moments)} 个')
    titles: set[str] = set()
    for index, moment in enumerate(moments, 1):
        item_problems = validate_moment(moment, avoid_titles=avoid_titles)
        if moment.title in titles:
            item_problems.append('title 与本组其他瞬间重复')
        titles.add(moment.title)
        problems.extend(f'第 {index} 个瞬间：{item}' for item in item_problems)
    return problems


def _require_json(content: object) -> dict:
    if not isinstance(content, str):
        raise ValueError('director content is not text')
    value = content.strip()
    value = re.sub(r'^```(?:json)?\s*', '', value, flags=re.I)
    value = re.sub(r'\s*```$', '', value)
    start, end = value.find('{'), value.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('director response has no JSON object')
    payload = json.loads(value[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError('director response is not an object')
    return payload


def _parse_moments(payload: dict) -> list[Moment]:
    raw = payload.get('moments')
    if not isinstance(raw, list):
        raise ValueError('moments is not an array')
    result = []
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError('moment is not an object')
        result.append(Moment(value.get('title'), value.get('shot_size'), value.get('text'), value.get('orientation')))
    return result


def _shot_requirement(count: int) -> str:
    if count == 1:
        return '不限，选择最适合这个场景的景别和机位。'
    return ('整组要像一次真实的拍摄：有交代环境的远景或全景，有互动的中景，有情绪的近景或特写；'
            '景别、机位和构图都要有变化，不要每张都是两人并排面对镜头。')


def _user_prompt(style: str, scene_text: str, count: int, shot_requirement: str, avoid_titles: list[str]) -> str:
    return (f'风格：{style}\n{scene_text}\n需要的镜头数量：{count}\n'
            f'景别要求：{shot_requirement}\n已拍过的镜头（不要重复）：{"、".join(avoid_titles) if avoid_titles else "无"}\n\n'
            f'请输出 JSON：{{"moments": [{{"title": "2～12 个汉字的标题", "shot_size": "{" | ".join(SHOT_SIZES)}", "orientation": "portrait | landscape", "text": "镜头描述，最多 {MAX_TEXT_LENGTH} 个字，以句号结尾"}}]}}\n'
            f'shot_size 含义：{SHOT_MEANINGS}。orientation 含义：portrait 竖版，landscape 横版。')


class MockDirector:
    _samples = (
        ('窗边转圈', 'full', 'landscape', '35mm 镜头，平视机位，全身。两人位于画面右侧三分线附近，左侧留出空间。新郎牵着新娘的手举过她头顶，新娘转到一半回头笑着看向镜头，场景的主光同时照亮两人和地面。'),
        ('回眸相依', 'medium', 'portrait', '50mm 镜头，略低机位，中景。新娘向前迈步后回头看向镜头，新郎轻扶她的手臂、侧身看着她，两人偏画面左侧，主光从侧前方照来。'),
        ('相视一笑', 'extreme_close', 'portrait', '105mm 镜头，平视机位，特写。两人脸颊相近，新娘忍不住先笑出来，新郎侧过脸看着她也笑了，主光从侧前方照来，背景完全虚化。'),
        ('额头相贴', 'close', 'portrait', '85mm 镜头，浅景深近景。两人额头轻轻相贴，新郎牵住新娘的手，新娘笑着看向镜头，主光从侧面照来，背景完全虚化。'),
    )

    def __init__(self):
        self.counter = 0

    def write_moments(self, style, scene_text, count, shot_requirement, avoid_titles):
        result = []
        used = set(avoid_titles)
        for index in range(count):
            title, shot, orientation, text = self._samples[(self.counter + index) % len(self._samples)]
            candidate = title
            suffix = 2
            while candidate in used:
                candidate = f'{title}{suffix}'
                suffix += 1
            used.add(candidate)
            result.append(Moment(candidate, shot, text, orientation))
        self.counter += count
        problems = validate_moments(result, count=count, avoid_titles=avoid_titles)
        if problems:
            raise DirectorInvalidOutput(problems)
        return result

    def rewrite_shot(self, style, scene_text, original_text, target_shot, orientation):
        title = SHOT_NAMES[target_shot] + '瞬间'
        moment = Moment(title, target_shot, original_text if original_text.endswith('。') else original_text + '。', orientation)
        problems = validate_moment(moment, target_shot=target_shot, target_orientation=orientation)
        if problems:
            raise DirectorInvalidOutput(problems)
        return moment


class Api88Director:
    def __init__(self, config: Config, *, model: str | None = None, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.model = model or config.director_model
        self.transport = transport

    def _call(self, user: str) -> list[Moment]:
        if not self.model:
            raise DirectorFailed('请先在设置中填写 AI 导演使用的文字模型')
        headers = {'Authorization': f'Bearer {self.config.api_key}', 'Content-Type': 'application/json'}
        url = self.config.base_url.rstrip('/') + '/v1/chat/completions'
        try:
            with httpx.Client(transport=self.transport, timeout=60, trust_env=False) as client:
                response = client.post(url, headers=headers, json={'model': self.model, 'messages': [
                    {'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': user}], 'temperature': 0.9})
        except Exception as exc:
            logger.warning('director transport failure model=%s error=%s', self.model, str(exc).replace(self.config.api_key, '[REDACTED]'))
            raise DirectorFailed('模型服务连接失败，请稍后重试') from None
        if response.status_code in (401, 402, 403):
            logger.warning('director provider auth failure status=%s', response.status_code)
            raise DirectorFailed(AUTH_MESSAGE)
        if response.status_code >= 400:
            logger.warning('director provider HTTP %s: %s', response.status_code, response.text[:1000].replace(self.config.api_key, '[REDACTED]'))
            raise DirectorFailed('文字模型服务暂时异常，请稍后重试')
        try:
            payload = response.json()
            content = payload['choices'][0]['message']['content']
            return _parse_moments(_require_json(content))
        except Exception:
            raise ValueError('invalid director JSON') from None

    def write_moments(self, style, scene_text, count, shot_requirement, avoid_titles):
        user = _user_prompt(style, scene_text, count, shot_requirement, avoid_titles)
        try:
            moments = self._call(user)
        except ValueError:
            moments = []
        problems = validate_moments(moments, count=count, avoid_titles=avoid_titles)
        if problems:
            feedback = '上面的输出不符合要求：' + '；'.join(problems) + '。请修正后重新输出完整 JSON。'
            try:
                moments = self._call(user + '\n\n' + feedback)
            except ValueError:
                raise DirectorInvalidOutput(problems) from None
            problems = validate_moments(moments, count=count, avoid_titles=avoid_titles)
        if problems:
            raise DirectorInvalidOutput(problems)
        return moments

    def rewrite_shot(self, style, scene_text, original_text, target_shot, orientation):
        user = (f'风格：{style}\n{scene_text}\n原来的镜头：{original_text}\n'
                f'请保持这个镜头里的瞬间（动作、互动、视线）和人物在场景中的位置不变，只把景别改为{SHOT_NAMES[target_shot]}，并相应调整机位、焦段和构图（例如远景让人物变小、交代环境，全景要交代脚下和站姿，近景只保留胸口以上）。画幅保持{ORIENTATION_NAMES[orientation]}。标题可以保留或微调。\n'
                f'只输出 JSON：{{"moments": [{{"title": "…", "shot_size": "{target_shot}", "orientation": "{orientation}", "text": "…"}}]}}')
        try:
            moments = self._call(user)
        except ValueError:
            moments = []
        problems = ['需要返回一个瞬间'] if len(moments) != 1 else validate_moment(moments[0], target_shot=target_shot, target_orientation=orientation)
        if problems:
            feedback = '上面的输出不符合要求：' + '；'.join(problems) + '。请修正后重新输出完整 JSON。'
            try:
                moments = self._call(user + '\n\n' + feedback)
            except ValueError:
                raise DirectorInvalidOutput(problems) from None
            problems = ['需要返回一个瞬间'] if len(moments) != 1 else validate_moment(moments[0], target_shot=target_shot, target_orientation=orientation)
        if problems:
            raise DirectorInvalidOutput(problems)
        return moments[0]


def moment_dict(moment: Moment) -> dict:
    return asdict(moment)
