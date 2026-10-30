"""AI director adapters and shared moment validation."""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass
from typing import Protocol

import httpx

from .api88 import AUTH_MESSAGE
from .aspect import ORIENTATION_NAMES, ORIENTATIONS
from .config import Config
from .prompts import DETAIL_SHOT

logger = logging.getLogger(__name__)

# Wide plus the four studio framings (V3.14). Works from V3.4–V3.11 may still carry medium / extreme_close / detail;
# they are read and shown but never written for new shots.
SHOT_SIZES = ('wide', 'full', 'three_quarter', 'half', 'close')
SHOT_NAMES = {'full': '全身', 'three_quarter': '七分身', 'half': '半身', 'close': '近景',
              'wide': '远景', 'medium': '中景', 'extreme_close': '特写', DETAIL_SHOT: '局部特写'}
SHOT_MEANINGS = 'wide 远景，full 全身，three_quarter 七分身，half 半身，close 近景'
FORBIDDEN_WORDS = (
    '眼睛', '眼型', '眼神清澈', '双眼皮', '单眼皮', '丹凤', '杏眼', '桃花眼', '睫毛', '眉毛', '眉形',
    '鼻子', '鼻梁', '鼻型', '嘴唇', '唇形', '红唇', '脸型', '瓜子脸', '鹅蛋脸', '圆脸', '尖下巴',
    '下颌', '颧骨', '酒窝', '雀斑', '皮肤', '肤色', '五官', '长相', '墨镜', '口罩',
)
MAX_TEXT_LENGTH = 220
SYSTEM_PROMPT = '''你是一位资深的婚纱摄影师兼导演，为一对新人设计照片。服装、视觉语言和可用的场景已由摄影团队确定，你要决定的是：每张照片在哪里拍、怎么拍。
这是交付给新人的婚纱照：两人的脸和婚纱是主角。大片感来自人和环境的比例、留白、层次和光影，不靠遮脸、极端机位或远处的小脸。

每张照片写成一段「镜头」，依次交代下面几项；机位、构图和光各用一两句话写清，把更多篇幅留给瞬间：
1. 机位与焦段：机位的高低和角度，用什么焦段（例如 35mm 交代环境，85mm 压缩背景、浅景深）。
2. 构图：竖版还是横版，两人在画面里的位置和大小，留白在哪里，有没有前景。以竖版为主；交代环境的远景、需要横向延展的画面（例如海平面、天际线、长廊）适合横版。
3. 人与场景：使用哪个场景（有多个场景变体时选一个），把它的编号填在 scene。所选场景的文字会原样放进最终的提示词，镜头里不用重复描写场景，只写两人所在的位置和与哪个场景元素发生关系。
4. 光：光的时段、色调、方向和明暗都照所选场景的文字写，场景没有写日落或傍晚时，就是白天、色温中性的自然光；写清这束光从哪个方向照到两人身上、同时照在周围的哪些环境上、哪里落在阴影里；摄影补光灯等器材不要出现在画面里，吊灯、烛台等场景里的装饰可以入画。
5. 瞬间：一个可拍摄的动作瞬间，写清动作、身体接触、互动、表情和视线落点。动作可以是并肩站立或坐下、对视、拥抱和依偎、牵手行走、额头相贴、亲吻、转圈或奔跑、抱起，也可以是提起裙摆、拖尾铺开、头纱被风吹起这类和婚纱的互动（只用服装里有的）。
6. 分别写新娘和新郎的面部朝向 facing：front、left 或 right。方向按画面所见判断，left 表示脸朝画面左侧，right 表示脸朝画面右侧。facing 不是 front 的人，镜头文字里要原样写出「新娘的脸朝画面左侧」「新郎的脸朝画面右侧」这样的一句，方向与 facing 一致；facing 是 front 的人不写这句。例如“两人面对面站，新娘在画面左边，新娘的脸朝画面右侧”，这时 bride_facing 是 right。

必须遵守：
1. 不描述任何五官或长相（例如眼睛、脸型、鼻子、嘴唇、皮肤），长相由参考照片决定。
2. 以两人面部清晰可见的镜头为主；需要氛围时，也可以拍背影、剪影、两人在画面中很小的远景，或让前景的花枝、头纱半遮住两人，这时要在镜头里明确写出「背影」「剪影」「两人在画面中很小」或怎样遮挡，并把 faces_clear 填为 false；其他镜头 faces_clear 填 true。
3. 不描写服装的款式和颜色，服装由摄影团队决定；动作可以和服装里有的头纱、裙摆、拖尾互动；光只能来自所选场景里的光源。
4. 动作自然、真实可拍，避免多人、复杂道具和夸张特技。
5. 表情以含蓄为主：浅笑、抿嘴笑、嘴角带笑、低头笑、安静对视，笑容可以微微露齿，但不要写成咧嘴大笑；整组的表情要有变化。一组里可以有一两个开怀大笑的瞬间，这时要明确写出「开怀大笑」。
6. 各个镜头彼此不同，也不要与「已拍过的镜头」重复。
7. 只写具体的拍摄决定，不要堆砌「高级感」「电影感」「唯美」这类空洞的形容词；面部清晰、曝光和遮挡由摄影团队另行把关，镜头里不用复述这些要求（例如「不遮脸」「亮部保留纹理」「不拍剪影」）。
8. 只输出 JSON，不要输出任何其他文字。

写法：焦段与机位 → 画幅、两人的位置和留白（有前景时写清前景是什么）→ 两人在场景里站在哪里 → 光从哪里来、照亮什么、哪里落在阴影里 → 动作、身体接触、表情和视线落点。每一项都写具体的决定，例如写「85mm 镜头，平视」，不写「合适的焦段」。'''


@dataclass(frozen=True)
class Moment:
    title: str
    shot_size: str
    text: str
    orientation: str = 'portrait'
    bride_facing: str = 'front'
    groom_facing: str = 'front'
    scene: int | None = None
    # False for atmosphere shots that do not show the faces clearly; True for drafts before V3.35.
    faces_clear: bool = True


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
                      avoid_titles: list[str], *, scene_count: int) -> list[Moment]: ...

    def rewrite_shot(self, style: str, scene_text: str, original_text: str, target_shot: str,
                     orientation: str) -> Moment: ...


def _cjk_or_basic_length(value: str) -> int:
    return len(value)


_FACING_ROLES = (('bride_facing', '新娘'), ('groom_facing', '新郎'))
_FACING_WORDS = {'left': '左', 'right': '右'}


def _facing_text_problems(text: str, facings: dict[str, str]) -> list[str]:
    """The facing picks which side photo is sent, so it must agree with what the text tells the image model."""
    problems = []
    for key, name in _FACING_ROLES:
        written = set(re.findall(name + r'的?脸朝画面([左右])', text))
        facing = facings.get(key)
        if facing in _FACING_WORDS:
            if written != {_FACING_WORDS[facing]}:
                problems.append(f'{key} 是 {facing}，text 里必须写「{name}的脸朝画面{_FACING_WORDS[facing]}侧」，且不能写别的方向')
        elif facing == 'front' and written:
            problems.append(f'{key} 是 front，text 里不能写「{name}的脸朝画面左侧/右侧」')
    return problems


def validate_moment(moment: Moment | dict, *, avoid_titles: list[str] = (), target_shot: str | None = None,
                    target_orientation: str | None = None, check_facing_text: bool = False,
                    scene_count: int | None = None) -> list[str]:
    """check_facing_text and scene_count apply to director output only: a user may change facing by hand,
    and drafts written before V3.34 have no scene."""
    if isinstance(moment, dict):
        title, shot_size, text = moment.get('title'), moment.get('shot_size'), moment.get('text')
        orientation = moment.get('orientation')
        bride_facing, groom_facing = moment.get('bride_facing'), moment.get('groom_facing')
        scene, faces_clear = moment.get('scene'), moment.get('faces_clear', True)
    else:
        title, shot_size, text, orientation = moment.title, moment.shot_size, moment.text, moment.orientation
        bride_facing, groom_facing = moment.bride_facing, moment.groom_facing
        scene, faces_clear = moment.scene, moment.faces_clear
    problems: list[str] = []
    if not isinstance(title, str) or not (2 <= _cjk_or_basic_length(title) <= 12) or not re.search(r'[\u3400-\u9fff]', title or ''):
        problems.append('标题必须为 2～12 个字符且至少包含一个汉字')
    if not isinstance(shot_size, str) or shot_size not in SHOT_SIZES:
        problems.append('shot_size 必须是 ' + '、'.join(SHOT_SIZES) + ' 之一')
    if orientation not in ORIENTATIONS:
        problems.append('orientation 必须是 portrait 或 landscape')
    if bride_facing not in ('front', 'left', 'right'):
        problems.append('bride_facing 必须是 front、left 或 right')
    if groom_facing not in ('front', 'left', 'right'):
        problems.append('groom_facing 必须是 front、left 或 right')
    if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH or not text.endswith('。'):
        problems.append(f'text 必须不超过 {MAX_TEXT_LENGTH} 个字符并以“。”结尾')
    if check_facing_text and isinstance(text, str):
        problems.extend(_facing_text_problems(text, {'bride_facing': bride_facing, 'groom_facing': groom_facing}))
    if scene_count is not None and (not isinstance(scene, int) or isinstance(scene, bool) or not 1 <= scene <= scene_count):
        problems.append(f'scene 必须是 1～{scene_count} 之间的场景编号')
    if not isinstance(faces_clear, bool):
        problems.append('faces_clear 必须是 true 或 false')
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


def validate_moments(moments: list[Moment], *, count: int | None = None, avoid_titles: list[str] = (),
                     scene_count: int | None = None) -> list[str]:
    """Validate a set the director wrote; facing must agree with the text."""
    problems: list[str] = []
    if count is not None and len(moments) != count:
        problems.append(f'需要 {count} 个瞬间，实际为 {len(moments)} 个')
    titles: set[str] = set()
    for index, moment in enumerate(moments, 1):
        item_problems = validate_moment(moment, avoid_titles=avoid_titles, check_facing_text=True, scene_count=scene_count)
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
        result.append(Moment(value.get('title'), value.get('shot_size'), value.get('text'), value.get('orientation'),
                             value.get('bride_facing'), value.get('groom_facing'), value.get('scene'), value.get('faces_clear')))
    return result


def _shot_requirement(count: int) -> str:
    if count == 1:
        return '不限，选择最适合这个场景的景别和机位。'
    # A wedding set: delivery shots first, then interaction, costume and at most a little atmosphere (V3.36).
    standard, costume, atmosphere = math.ceil(count / 3), count // 6, count // 8
    interaction = count - standard - costume - atmosphere
    close_limit, foreground_limit = max(1, count // 10), max(1, count // 6)  # kisses and lifts are hard to render
    parts = [f'整组 {count} 张要像一次真实的婚纱拍摄，按下面的构成安排：',
             f'标准照 {standard} 张：两人正面站立或坐着，一起看向镜头，婚纱完整入画；',
             f'互动 {interaction} 张：以两人彼此互动为主，要有身体接触和动作，例如拥抱、依偎、额头相贴、牵手行走或奔跑、转圈、抱起、亲吻；'
             f'其中至少 1 张拥抱、1 张在走动或转圈中抓拍，亲吻、抱起各最多 {close_limit} 张；']
    if costume:
        parts.append(f'婚纱表现 {costume} 张：提起裙摆、拖尾铺开或头纱被风吹起，只用服装里有的；')
    parts.append(f'氛围镜头 {atmosphere} 张：背影、剪影或两人在画面中很小，faces_clear 填 false；' if atmosphere else '这组不安排氛围镜头；')
    parts.append('景别上既有交代环境的远景（用广角靠近两人拍，身后是大片的环境，两人仍能看清神情），也有中景和情绪近景；'
                 '可以有 1 张用 85～135mm 把背景压缩到两人身后，人物仍要足够大；'
                 f'前景最多 {foreground_limit} 张且不遮脸，明显的高处俯拍最多 1 张；景别、机位和构图都要有变化，各类镜头的先后顺序不要固定。')
    return ''.join(parts)


def _user_prompt(style: str, scene_text: str, count: int, shot_requirement: str, avoid_titles: list[str]) -> str:
    return (f'风格：{style}\n{scene_text}\n需要的镜头数量：{count}\n'
            f'景别要求：{shot_requirement}\n已拍过的镜头（不要重复）：{"、".join(avoid_titles) if avoid_titles else "无"}\n\n'
            f'请输出 JSON：{{"moments": [{{"title": "2～12 个汉字的标题", "scene": 所选场景的编号, "faces_clear": true | false, "shot_size": "{" | ".join(SHOT_SIZES)}", "orientation": "portrait | landscape", "bride_facing": "front | left | right", "groom_facing": "front | left | right", "text": "镜头描述，最多 {MAX_TEXT_LENGTH} 个字，以句号结尾"}}]}}\n'
            f'scene 是数字，只有一个场景时填 1。faces_clear：两人面部清晰可见时为 true，背影、剪影、人物很小或被遮挡的氛围镜头为 false。shot_size 含义：{SHOT_MEANINGS}。orientation 含义：portrait 竖版，landscape 横版。'
            f'facing 按画面所见判断，left 是脸朝画面左侧，right 是脸朝画面右侧；不是 front 时，text 里要写出「新娘的脸朝画面左侧」这样的一句，方向与 facing 一致。')


class MockDirector:
    _samples = (
            ('窗边转圈', 'full', 'landscape', 'front', 'front', '35mm 镜头，平视机位，全身。两人位于画面右侧三分线附近，左侧留出空间。新郎牵着新娘的手举过她头顶，新娘转到一半回头笑着看向镜头，场景的主光同时照亮两人和地面。'),
        ('回眸相依', 'three_quarter', 'portrait', 'front', 'left', '50mm 镜头，略低机位，七分身。新娘向前迈步后回头看向镜头，新郎轻扶她的手臂、侧身看着她，新郎的脸朝画面左侧，两人偏画面左侧，主光从侧前方照来。'),
        ('相视浅笑', 'half', 'portrait', 'right', 'left', '65mm 镜头，平视机位，半身。两人面对面站着，新娘的脸朝画面右侧，新郎的脸朝画面左侧，新郎轻握新娘的双手，两人相视浅笑，主光从侧前方照来，背景虚化。'),
        ('额头相贴', 'close', 'portrait', 'front', 'front', '85mm 镜头，浅景深近景。两人额头轻轻相贴，新郎牵住新娘的手，新娘笑着看向镜头，主光从侧面照来，背景完全虚化。'),
    )

    def __init__(self):
        self.counter = 0

    def write_moments(self, style, scene_text, count, shot_requirement, avoid_titles, *, scene_count):
        result = []
        used = set(avoid_titles)
        for index in range(count):
            title, shot, orientation, bride_facing, groom_facing, text = self._samples[(self.counter + index) % len(self._samples)]
            candidate = title
            suffix = 2
            while candidate in used:
                candidate = f'{title}{suffix}'
                suffix += 1
            used.add(candidate)
            scene = (self.counter + index) % scene_count + 1
            result.append(Moment(candidate, shot, text, orientation, bride_facing, groom_facing, scene))
        self.counter += count
        problems = validate_moments(result, count=count, avoid_titles=avoid_titles, scene_count=scene_count)
        if problems:
            raise DirectorInvalidOutput(problems)
        return result

    def rewrite_shot(self, style, scene_text, original_text, target_shot, orientation):
        title = SHOT_NAMES[target_shot] + '瞬间'
        text = original_text if original_text.endswith('。') else original_text + '。'
        # Keep the facing the original text states, so the rewrite stays consistent.
        facings = {key: next((facing for facing, word in _FACING_WORDS.items() if f'{name}的脸朝画面{word}' in text), 'front')
                   for key, name in _FACING_ROLES}
        moment = Moment(title, target_shot, text, orientation, facings['bride_facing'], facings['groom_facing'])
        problems = validate_moment(moment, target_shot=target_shot, target_orientation=orientation, check_facing_text=True)
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

    def write_moments(self, style, scene_text, count, shot_requirement, avoid_titles, *, scene_count):
        user = _user_prompt(style, scene_text, count, shot_requirement, avoid_titles)
        try:
            moments = self._call(user)
        except ValueError:
            moments = []
        problems = validate_moments(moments, count=count, avoid_titles=avoid_titles, scene_count=scene_count)
        if problems:
            feedback = '上面的输出不符合要求：' + '；'.join(problems) + '。请修正后重新输出完整 JSON。'
            try:
                moments = self._call(user + '\n\n' + feedback)
            except ValueError:
                raise DirectorInvalidOutput(problems) from None
            problems = validate_moments(moments, count=count, avoid_titles=avoid_titles, scene_count=scene_count)
        if problems:
            raise DirectorInvalidOutput(problems)
        return moments

    def rewrite_shot(self, style, scene_text, original_text, target_shot, orientation):
        user = (f'风格：{style}\n{scene_text}\n原来的镜头：{original_text}\n'
                f'请保持这个镜头里的瞬间（动作、互动、视线）和人物在场景中的位置不变，只把景别改为{SHOT_NAMES[target_shot]}，并相应调整机位、焦段和构图（例如远景用广角靠近两人拍、身后是大片的环境、两人仍能看清神情，全身需要交代脚下、站姿和周围环境，近景只保留上半身和面部朝向，背景更虚化）。画幅保持{ORIENTATION_NAMES[orientation]}。标题可以保留或微调。\n'
                f'只输出 JSON：{{"moments": [{{"title": "…", "shot_size": "{target_shot}", "orientation": "{orientation}", "faces_clear": true | false, "bride_facing": "front | left | right", "groom_facing": "front | left | right", "text": "…"}}]}}。facing 按画面所见；不是 front 时，text 里要写出「新娘的脸朝画面左侧」这样的一句，方向与 facing 一致。')
        try:
            moments = self._call(user)
        except ValueError:
            moments = []
        problems = ['需要返回一个瞬间'] if len(moments) != 1 else validate_moment(moments[0], target_shot=target_shot, target_orientation=orientation, check_facing_text=True)
        if problems:
            feedback = '上面的输出不符合要求：' + '；'.join(problems) + '。请修正后重新输出完整 JSON。'
            try:
                moments = self._call(user + '\n\n' + feedback)
            except ValueError:
                raise DirectorInvalidOutput(problems) from None
            problems = ['需要返回一个瞬间'] if len(moments) != 1 else validate_moment(moments[0], target_shot=target_shot, target_orientation=orientation, check_facing_text=True)
        if problems:
            raise DirectorInvalidOutput(problems)
        return moments[0]


def moment_dict(moment: Moment) -> dict:
    return asdict(moment)
