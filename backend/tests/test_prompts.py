"""Prompt composition contract for V3 prompts."""

import re
from pathlib import Path

import pytest

from app.prompts import (
    GENERAL_AVOID,
    POSES,
    QUALITY_PARAGRAPH,
    LIGHTING_RULES,
    STYLES,
    STYLE_BY_ID,
    BUZZ_HAIR,
    compose_identity_boost_moment_prompt,
    compose_moment_prompt,
    compose_photo_prompt,
    BRIDE_HAIR_LENGTHS,
    GROOM_HAIR_LENGTHS,
    Hair,
    MAKEUP_LEVELS,
    PREVIEW_MOMENT,
    RETIRED_STYLE_IDS,
    Makeup,
    compose_preview_prompt,
    compose_look_prompt,
    compose_bride_hair_prompt,
    compose_groom_hair_prompt,
    compose_bride_makeup_prompt,
    HAIRSTYLES,
    MAKEUP_COLORS,
    STYLE_MAKEUP_COLORS,
    STYLE_MAKEUP_FINISH,
    UPDO_STYLES,
    is_styled,
    is_faceless_shot,
    has_makeup,
    scene_from_prompt,
    shot_text_from_prompt,
)


IDENTITY_TEXT = (
    '图片角色：第一张图是新娘的身份参考，第二张图是新郎的身份参考。'
    '严格保持两人的长相：脸型、五官比例、眼型、鼻型、唇形、肤色、发际线和年龄感都与第一、二张参考照片一致；'
    '不要美颜、瘦脸、磨皮或改变五官。允许改变的只有发型造型、妆容、服装、姿态、场景和光线。'
    '风格要求与长相冲突时，以长相为准。'
)
PREVIEW_INSERTION = '第三张图是风格参考，只参考它的场景、服装、光线和色调，不参考其中的人脸、长相和构图。'
FORBIDDEN_TERMS = ('油画质感', '韩系', '专业婚纱摄影效果', '侧面视角', '丹凤', '杏眼', '高鼻')
EXAMPLE_SHOT = (
    '50mm 镜头，平视机位，中景。浅灰色的墙面前，阳光穿过画面右侧的百叶窗，在墙面和两人身上落下斜向的条纹光影。'
    '两人位于画面左侧三分线附近，右侧留出布满条纹光影的墙面。新郎从身后轻轻环住新娘，新娘笑着看向镜头，新郎侧过脸看她。'
    '同一束光照亮两人和墙面，条纹落在肩膀和衣服上。'
)


def assert_six_paragraphs(prompt: str, shot_label: str):
    paragraphs = prompt.split('\n\n')
    assert len(paragraphs) == 6
    assert paragraphs[0] == '竖构图，3:4 比例的写实婚纱照。'
    assert paragraphs[1].startswith('图片角色：')
    assert paragraphs[2].startswith('服装与风格：')
    assert paragraphs[3].startswith(shot_label)
    assert paragraphs[4] == QUALITY_PARAGRAPH
    assert paragraphs[5].startswith('避免：')
    assert paragraphs[5].endswith('。')


DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()


def doc_block(heading: str) -> str:
    """First ```text block after a docs/prompts.md heading."""
    return re.search(re.escape(heading) + r'.*?```text\n(.*?)\n```', DOC, re.S).group(1)


def test_full_example_matches_docs_verbatim():
    expected = doc_block('## 5. 完整示例')
    # V7.40: the example's shot ends with the likeness reminder; the director's own text is what comes before it.
    assert compose_moment_prompt('cinematic_light', shot_text_from_prompt(expected), '3:4', scene=2,
                                 styled=True) == expected


def test_styled_shot_prompts_copy_the_looks_hair_and_makeup_and_match_docs():
    identity = compose_preview_prompt('korean_minimal', '3:4', styled=True).split('\n\n')[1]
    assert identity == doc_block('**V7.6 起：参考图是两人带妆的造型照')
    # V7.6: the look's makeup strength carries over; wide shots never enlarge people to show it.
    assert '不将已有婚礼妆淡化为素颜或日常淡妆' in identity and '不为展示妆容移动机位、放大人物' in identity
    # V7.5: the structure that must not change, listed like Codex suggested, and smiles may still move the face.
    assert '保留本人固有的骨相与脸部比例' in identity and '可见耳朵的位置和形态' in identity and '不减龄' in identity
    assert is_styled(compose_preview_prompt('korean_minimal', '3:4', styled=True))
    assert not is_styled(compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup('natural', 'natural'), hair=Hair('long', 'short')))
    # V7.0: the looks wear the makeup, so a shot from looks takes no levels.
    with pytest.raises(ValueError):
        compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup('natural', 'natural'), styled=True)
    assert '造型照就是两人婚礼当天化好妆、做好发型后的样子' in identity
    assert '造型照用于确定两人的长相、发型和妆容' in identity and '不额外美颜或磨皮' in identity and '不瘦脸，不重塑骨相' in identity
    assert '基础肤色及其冷暖底调' in identity and '造型照的均匀照明不作为拍摄布光' in identity
    assert '明暗变化不改变造型照确定的肤色底色与妆色' in identity
    # V7.3: the expression follows the shot, not the neutral look; no stiff "stand tall".
    assert '不照搬造型照中的表情和视线' in identity and '两人体态自然舒展' in identity and '挺拔' not in identity
    # The colours and finish of the style paragraph are the look's; V6.4's two sentences on visibility and light stay.
    assert '与「服装与风格」中的新娘妆色和新娘妆面一致' in identity
    assert '不为展示妆容移动机位、放大人物或改变眼型、唇形' in identity and '场景光线只改变合理的受光明暗与反光' in identity
    assert not any(term in identity for term in ('底妆：', '卧蚕', '比原照片白', '比造型照白', '比日常妆更浓', '眼线：'))
    for style in STYLES:
        prompt = compose_moment_prompt(style['id'], EXAMPLE_SHOT, '3:4', scene=1, styled=True)
        assert_six_paragraphs(prompt, '镜头：')
        # The hair is on the looks: the style paragraph names no hairstyle (V4.0); the updo styles add their
        # headwear on top of the look's hairstyle (V5.0); the bride's colour family (V6.0) and finish (V6.4) are the
        # style's, the same as her look's (V7.0).
        paragraph = prompt.split('\n\n')[2]
        clothes = style['costume'].split('妆容方向：')[0]
        color = MAKEUP_COLORS[[item.id for item in MAKEUP_COLORS].index(STYLE_MAKEUP_COLORS[style['id']][0])]
        assert '妆容方向' not in paragraph and '发型：' not in paragraph
        assert paragraph.startswith('服装与风格：' + clothes + '新娘妆色：' + color.text + '新娘妆面：' +
                                    STYLE_MAKEUP_FINISH[style['id']] + UPDO_STYLES.get(style['id'], '') + style['look'])
        # V6.4: every style has a finish, listed in the docs.
        assert f"| {style['name']} `{style['id']}` | `{STYLE_MAKEUP_FINISH[style['id']]}` |" in DOC
        assert not has_makeup(prompt)
    sides = compose_moment_prompt('korean_minimal', EXAMPLE_SHOT, '3:4', styled=True, with_preview=True,
                                  side_references=[('bride', 'left'), ('groom', 'right')]).split('\n\n')[1]
    assert sides.startswith('图片角色：第一张图是新娘的造型照，第二张图是新郎的造型照，第三张图是新娘脸朝画面左侧时的造型照，'
                            '第四张图是新郎脸朝画面右侧时的造型照。第五张图是风格参考')
    boosted = compose_identity_boost_moment_prompt('korean_minimal', EXAMPLE_SHOT, '3:4', scene=1, styled=True)
    assert is_styled(boosted) and boosted.split('\n\n')[1].endswith('宁可画面朴素，也要像本人。')
    # Prompts of V4.0–V5.2 and V6.0–V6.4 still count as styled.
    assert is_styled('竖构图。\n\n' + doc_block('**V4.0～V5.2：参考图是两人带妆的造型照'))
    assert is_styled('竖构图。\n\n' + doc_block('**V7.0～V7.2：'))
    assert is_styled('竖构图。\n\n' + doc_block('**V7.3～V7.4：'))
    assert is_styled('竖构图。\n\n' + doc_block('**V7.5：'))
    bare = doc_block('**V6.0～V6.4：参考图是只做了发型的素颜造型照')
    assert is_styled('竖构图。\n\n' + bare) and '卧蚕：微弱提亮，不改变眼睛大小' in bare


def test_look_prompts_cover_every_hairstyle_level_and_style_and_match_docs():
    assert doc_block('新娘正面照一次编辑的写法') == compose_look_prompt('bride', 'low_bun', 'natural', style_id='korean_minimal')
    # V7.5: the bride's front look is makeup first, then only the hair on the made-up photo.
    makeup = compose_bride_makeup_prompt('natural', 'korean_minimal')
    assert '这一步只化妆、修整皮肤和换背景，头发保持原照片的样子' in makeup and '发型：' not in makeup
    assert '新娘妆面：' + STYLE_MAKEUP_FINISH['korean_minimal'] in makeup and '保留本人固有的骨相与脸部比例' in makeup
    hair = compose_bride_hair_prompt('low_bun')
    assert '这一步只改头发' in hair and '不再上妆、不再提白、不再修皮肤' in hair and '新娘妆色' not in hair
    assert hair.split('\n\n')[1].startswith('发型：' + next(item.text for item in HAIRSTYLES if item.id == 'low_bun'))
    assert '改变头发不授权改变脸部、耳朵或颅骨' in hair and '头发不遮挡眼睛' not in hair
    assert makeup.split('\n\n')[0] in DOC and hair.split('\n\n')[0] in DOC
    assert doc_block('新郎脸朝画面左侧，`keep`') == compose_look_prompt('groom', 'keep', 'natural', facing='left')
    side = compose_look_prompt('bride', 'low_bun', 'natural', style_id='hanfu', facing='right')
    # V7.46: the side photo is sent alone; the styling is in the words.
    assert '头发按下面的发型重新画，不照搬照片里的头发。' in side and '第一张图' not in side and '第二张图' not in side
    assert '新娘妆面：' + STYLE_MAKEUP_FINISH['hanfu'] in side
    # V7.0: a bride look is made for one style; the groom's serves every style.
    with pytest.raises(ValueError):
        compose_look_prompt('bride', 'low_bun', 'natural')
    with pytest.raises(ValueError):
        compose_look_prompt('groom', 'keep', 'natural', style_id='korean_minimal')
    # V5.2: updos say what the front view shows, since a bun at the back alone left the hair down.
    for hairstyle in HAIRSTYLES:
        if hairstyle.updo:
            assert '露出' in hairstyle.text
            assert 'short' in hairstyle.lengths or '肩上没有' in hairstyle.text
        assert f"| `{hairstyle.id}` {hairstyle.name} | {'、'.join(f'`{length}`' for length in hairstyle.lengths)} | " \
               f"{'是' if hairstyle.updo else '否'} | `{hairstyle.text}` |" in DOC
        # Makeup levels never change the selected hairstyle, including its finish.
        front = (compose_bride_hair_prompt(hairstyle.id) if hairstyle.role == 'bride'
                 else compose_groom_hair_prompt(hairstyle.id))
        for level in MAKEUP_LEVELS:
            style_id = 'korean_minimal' if hairstyle.role == 'bride' else None
            prompt = compose_look_prompt(hairstyle.role, hairstyle.id, level, style_id=style_id)
            paragraphs = prompt.split('\n\n')
            assert len(paragraphs) == 4
            assert paragraphs[1].startswith('发型：' + hairstyle.text + '。')
            assert paragraphs[1] == front.split('\n\n')[1]
            # Natural texture applies to every hairstyle without forcing volume on sleek styles.
            assert '毛发质感：真实自然的毛发结构' in paragraphs[1]
            assert '蓬松度和服帖程度服从所选发型' in paragraphs[1]
            assert '不规则的交叠、疏密变化与局部空隙' in paragraphs[1]
            # V5.1: nothing asks the edit to keep the photo's hair. V7.5: changing it licenses no change to the face.
            assert '头发按下面的发型重新造型。' in paragraphs[0] and '大胆' not in prompt and '完全不同' not in prompt
            assert '改变头发不授权改变脸部' in paragraphs[1] and '保留本人固有的骨相与脸部比例' in paragraphs[0]
            assert not any(term in prompt for term in ('头肩构图', '长短与原照片一致', '保留少量细软的碎发'))
            assert '头发部分完全重新画' in paragraphs[1] and '没有任何翘起的细碎毛发' not in paragraphs[1]
            # V7.0: the look wears the makeup and retouching of the level, with the V6.4 limits.
            makeup = paragraphs[2]
            # V7.6: eyeliner only on the lash line, false lashes on the own lash roots; still no nose shadow.
            assert '眼线只沿上睫毛根部，不外延、不上挑、不拉长眼尾' in makeup and '不另画鼻影和高光亮条' in makeup
            assert '脸和脖子的肤色衔接自然' in makeup
            assert '比原照片白' in makeup and '比造型照白' not in makeup
            assert paragraphs[3].startswith('背景和光线：背景换成干净均匀的浅灰色纯色背景；柔和均匀的正面光')
            if hairstyle.role == 'bride':
                assert '新娘婚礼当天化好妆' in paragraphs[0]
                for part in ('底妆：', '眉妆：', '眼影：', '睫毛：', '腮红：', '唇妆：'):
                    assert part in makeup
                assert '卧蚕：微弱提亮，不改变眼睛大小' in makeup and '修容：极轻微的光影修饰，不重塑骨骼' in makeup
                assert '比日常妆更浓' not in makeup and '不画卧蚕' not in makeup
                # V7.6: brows are groomed by bounded actions, never reshaped; makeup strength is written per part.
                assert '只清理原眉主体轮廓外零散的杂毛' in makeup and '已整齐处保持原样' in makeup
                assert '眉头、眉峰、眉尾的位置及主体粗细、弧度和倾斜角度不变' in makeup
                for part in ('眼线：沿本人上睫毛根部', '假睫毛：在本人上睫毛根部', '不改变眼型和眼睛大小'):
                    assert part in makeup
                assert '不以原照的素颜或淡妆状态限制目标妆容' in makeup and '修眉' not in makeup and '标准眉' not in makeup
                # The level's text is the docs' level table (section 8.5).
                assert '`' + makeup.removeprefix('妆容和精修：').split('。新娘妆色：')[0] + '`' in DOC
            else:
                assert '新娘妆色' not in makeup and '只清理原眉主体轮廓外零散的杂毛' in makeup and '不补画、不加浓' in makeup
                assert '`' + makeup.removeprefix('皮肤和精修：').split('。精修和妆容')[0] + '`' in DOC
    for style in STYLES:
        color = MAKEUP_COLORS[[item.id for item in MAKEUP_COLORS].index(STYLE_MAKEUP_COLORS[style['id']][0])]
        makeup = compose_look_prompt('bride', 'low_bun', 'natural', style_id=style['id']).split('\n\n')[2]
        assert '新娘妆色：' + color.text + '新娘妆面：' + STYLE_MAKEUP_FINISH[style['id']] in makeup
    for color in MAKEUP_COLORS:
        assert f'| `{color.id}` {color.name} | `{color.text}` |' in DOC
    names = {color.id: color.name for color in MAKEUP_COLORS}
    assert set(STYLE_MAKEUP_COLORS) == {style['id'] for style in STYLES}
    for style in STYLES:
        primary, alternates = STYLE_MAKEUP_COLORS[style['id']]
        assert f"| {style['name']} `{style['id']}` | {names[primary]} | {'、'.join(names[item] for item in alternates)} |" in DOC
    for style_id, text in UPDO_STYLES.items():
        assert f'| `{style_id}` | `{text}` |' in DOC


def test_shared_paragraphs_and_costumes_match_docs_verbatim():
    assert doc_block('### 2.5 画面质感') == QUALITY_PARAGRAPH
    assert LIGHTING_RULES in QUALITY_PARAGRAPH
    assert doc_block('通用避免项：') == GENERAL_AVOID
    for style in STYLES:
        assert doc_block(f" `{style['id']}`\n") == style['costume']
        # Strength comes from the project level; a style only sets the hair and the makeup colors (V3.39).
        assert '淡妆' not in style['costume'] and '妆容方向：' in style['costume']
        # Hairstyles live in the per-length table, not in the costume; how tidy they get is set by level (V3.45).
        assert '发型' not in style['costume'] and '参考照片' not in style['costume']
        assert set(style['hair']['bride']) == set(BRIDE_HAIR_LENGTHS) and set(style['hair']['groom']) == set(GROOM_HAIR_LENGTHS)
        # V3.9: side-swept at most for the groom, never a full slicked-back look or a fully bared forehead.
        for text in style['hair']['groom'].values():
            assert not any(term in text for term in ('大背头', '油头', '露出额头', '全露额')), text
            assert '背头' not in text.replace('侧背头', ''), text
        for person in ('bride', 'groom'):
            for text in style['hair'][person].values():
                assert text in DOC, text
        # A style sets makeup colours only, never anything that reshapes the face (V3.41).
        direction = style['costume'].split('妆容方向：')[1]
        assert not any(term in direction for term in ('眉', '眼线', '修容', '上挑', '卧蚕', '鼻影'))


def test_preview_and_legacy_poses_use_the_first_variant_and_director_shots_their_chosen_one():
    for style in STYLES:
        scene = '场景：' + style['variants'][0][1]
        assert compose_preview_prompt(style['id'], '3:4').split('\n\n')[2] == style['scene_prompt'] + scene
        assert compose_photo_prompt(style['id'], 'pose_01', '3:4').split('\n\n')[2] == style['scene_prompt'] + scene
        last = len(style['variants'])
        chosen = compose_moment_prompt(style['id'], EXAMPLE_SHOT, '3:4', scene=last)
        assert chosen.split('\n\n')[2] == style['scene_prompt'] + '场景：' + style['variants'][-1][1]
        assert scene_from_prompt(style['id'], chosen) == last
        # Director shots before V3.34 have no scene and keep their old text.
        legacy = compose_moment_prompt(style['id'], EXAMPLE_SHOT, '3:4')
        assert legacy.split('\n\n')[2] == style['scene_prompt']
        assert scene_from_prompt(style['id'], legacy) is None
        boosted = compose_identity_boost_moment_prompt(style['id'], EXAMPLE_SHOT, '3:4', scene=last)
        assert scene_from_prompt(style['id'], boosted) == last


def test_active_samples_have_distinct_documented_shots_and_respect_the_requested_aspect():
    active = [style for style in STYLES if style['id'] not in RETIRED_STYLE_IDS]
    assert len({style['preview'] for style in active}) == len(active)
    for style in active:
        assert style['preview'] in DOC and style['shooting'] in DOC
        for ratio, orientation in [('3:4', '竖构图'), ('4:3', '横构图')]:
            paragraphs = compose_preview_prompt(style['id'], ratio, styled=True).split('\n\n')
            assert paragraphs[0].startswith(orientation)
            assert paragraphs[3].startswith(style['preview']) and paragraphs[3] != PREVIEW_MOMENT
            assert '两人的脸正对镜头' in paragraphs[3]
            assert '\n\n'.join(paragraphs) == compose_moment_prompt(
                style['id'], style['preview'].removeprefix('镜头：'), ratio, scene=1, styled=True)
            assert '竖版' not in paragraphs[3] and '横版' not in paragraphs[3]
            # A single image never receives the instructions for arranging an entire set.
            assert style['shooting'] not in '\n\n'.join(paragraphs)
    for style_id in RETIRED_STYLE_IDS:
        assert compose_preview_prompt(style_id, '3:4').split('\n\n')[3] == PREVIEW_MOMENT


def test_preview_reference_is_inserted_after_groom_sentence_and_keeps_identity_rules():
    identity = compose_photo_prompt(
        'korean_minimal', 'pose_01', '3:4', with_preview=True,
    ).split('\n\n')[1]
    marker = '第二张图是新郎的身份参考。'
    assert identity == IDENTITY_TEXT.replace(marker, marker + PREVIEW_INSERTION, 1)
    assert identity.index(PREVIEW_INSERTION) == identity.index(marker) + len(marker)
    assert identity.replace(PREVIEW_INSERTION, '') == IDENTITY_TEXT


def test_side_identity_references_are_described_after_fronts():
    identity = compose_moment_prompt(
        'korean_minimal', EXAMPLE_SHOT, '3:4',
        side_references=[('bride', 'left'), ('groom', 'right')],
    ).split('\n\n')[1]
    assert identity.startswith('图片角色：第一张图是新娘的身份参考，第二张图是新郎的身份参考，第三张图是新娘脸朝画面左侧时的样子，第四张图是新郎脸朝画面右侧时的样子。')
    assert '与新娘、新郎的参考照片一致' in identity
    with_preview = compose_moment_prompt(
        'korean_minimal', EXAMPLE_SHOT, '3:4', with_preview=True,
        side_references=[('bride', 'left')],
    ).split('\n\n')[1]
    assert with_preview.startswith('图片角色：第一张图是新娘的身份参考，第二张图是新郎的身份参考，第三张图是新娘脸朝画面左侧时的样子。'
                                   '第四张图是风格参考，只参考它的场景、服装、光线和色调，不参考其中的人脸、长相和构图。严格保持两人的长相')


def test_every_legacy_pose_and_reference_mode_has_six_paragraphs():
    for style in STYLES:
        for pose in POSES:
            for with_preview in (False, True):
                prompt = compose_photo_prompt(style['id'], pose['id'], '3:4', with_preview=with_preview)
                assert_six_paragraphs(prompt, '动作瞬间：')
                assert prompt.split('\n\n')[3] == pose['prompt_template']
                assert not any(term in prompt for term in FORBIDDEN_TERMS)


def test_aspect_ratio_mapping():
    assert compose_preview_prompt('flower_house', '3:4').startswith('竖构图，3:4 比例的写实婚纱照。')
    assert compose_preview_prompt('flower_house', '4:3').startswith('横构图，4:3 比例的写实婚纱照。')
    assert compose_preview_prompt('flower_house', '1:1').startswith('方构图，1:1 比例的写实婚纱照。')


@pytest.mark.parametrize('aspect_ratio', ['', '3/4', '0:3', '3:0', '3:4:1', '3:4 ', None])
def test_malformed_aspect_ratio_raises(aspect_ratio):
    with pytest.raises(ValueError):
        compose_preview_prompt('flower_house', aspect_ratio)


def test_preview_and_moment_prompts_keep_exactly_six_paragraphs_and_avoid_disallowed_terms():
    for style in STYLES:
        preview = compose_preview_prompt(style['id'], '3:4')
        assert_six_paragraphs(preview, '镜头：')
        assert not any(term in preview for term in FORBIDDEN_TERMS)
        for with_preview in (False, True):
            prompt = compose_moment_prompt(style['id'], EXAMPLE_SHOT, '3:4', with_preview=with_preview)
            assert_six_paragraphs(prompt, '镜头：')
            assert not any(term in prompt for term in FORBIDDEN_TERMS)


def test_big_laughs_are_not_banned_in_the_avoid_paragraph():
    # The quality paragraph allows shots that say 开怀大笑; an avoid term would override them.
    for style in STYLES:
        avoid = compose_preview_prompt(style['id'], '3:4').split('\n\n')[5]
        assert '大笑' not in avoid and '露齿' not in avoid


def test_general_avoid_terms_are_used_before_style_specific_avoid_terms():
    for style in STYLES:
        avoid = compose_preview_prompt(style['id'], '3:4').split('\n\n')[5]
        assert avoid.startswith(f'避免：{GENERAL_AVOID}、')
        assert avoid.endswith('。')


def test_shot_text_is_read_from_v3_and_legacy_v2_snapshots():
    v3 = compose_moment_prompt('korean_minimal', EXAMPLE_SHOT, '3:4')
    assert shot_text_from_prompt(v3) == EXAMPLE_SHOT
    v2 = '\n\n'.join(('竖构图，3:4 比例的写实婚纱照。', IDENTITY_TEXT, '服装与场景光线：旧风格。',
                      '动作瞬间：两人并肩牵手向镜头迈出一小步。', '避免：旧避免项。'))
    assert shot_text_from_prompt(v2) == '两人并肩牵手向镜头迈出一小步。'


def test_identity_boost_moment_prompt_keeps_quality_and_extends_last_avoid_paragraph():
    paragraphs = compose_identity_boost_moment_prompt('korean_minimal', EXAMPLE_SHOT, '3:4').split('\n\n')
    assert paragraphs[3].endswith('，两人面部正对或接近正对镜头，面部完整、清晰、占画面较大比例。')
    assert paragraphs[4] == QUALITY_PARAGRAPH
    assert paragraphs[5].endswith('、侧脸、低头、头发遮挡眼睛。')


def test_director_shots_name_costume_and_look_but_no_scene():
    for style in STYLES:
        paragraph = compose_moment_prompt(style['id'], '两人相视而笑。', '3:4').split('\n\n')[2]
        assert paragraph == '服装与风格：' + style['costume'] + style['look']


def test_atmosphere_shots_get_their_own_quality_and_drop_conflicting_avoid_terms():
    for style in STYLES:
        normal = compose_moment_prompt(style['id'], EXAMPLE_SHOT, '3:4', scene=1).split('\n\n')
        atmosphere = compose_moment_prompt(style['id'], EXAMPLE_SHOT, '3:4', scene=1, faces_clear=False).split('\n\n')
        assert normal[4] == QUALITY_PARAGRAPH and '两人面部清晰可辨' in normal[4]
        assert '两人面部清晰可辨' not in atmosphere[4] and '氛围照片' in atmosphere[4]
        assert '鼻翼、眼窝、下颌与颈部的自然阴影' in normal[4]
        assert '鼻翼、眼窝、下颌与颈部的自然阴影' not in atmosphere[4]
        assert LIGHTING_RULES in atmosphere[4]
        assert '面部被遮挡' in normal[5] and '面部被遮挡' not in atmosphere[5]
        assert '剪影' not in atmosphere[5].replace('、', ' ').split()
        assert atmosphere[:4] == normal[:4]
        assert is_faceless_shot('\n\n'.join(atmosphere)) and not is_faceless_shot('\n\n'.join(normal))
    # Atmosphere shots stored with an older quality paragraph are still recognised.
    older = '\n\n'.join(atmosphere).replace('皮肤干净细腻、有真实纹理', '皮肤保留真实质感')
    assert older != '\n\n'.join(atmosphere) and is_faceless_shot(older)
    # The word alone in the shot does not count.
    assert not is_faceless_shot(compose_moment_prompt('seaside_sunset', '逆光拍摄，不拍剪影。', '3:4', scene=1))


def test_makeup_and_retouching_are_drawn_in_the_identity_paragraph():
    allowed = '允许改变的只有发型造型、妆容、服装、姿态、场景和光线。'
    bride = {'light': '新娘化清透的新娘妆，做基础的精修：皮肤比参考照片白半个到一个色号',
             'natural': '新娘化标准的婚纱照新娘妆，按婚纱照成片精修：皮肤白皙透亮，比参考照片白一到两个色号',
             'refined': '新娘化精致的影楼新娘妆，按影楼成片精修：皮肤非常白皙透亮，比参考照片白两个色号'}
    groom = {'light': '新郎不化妆，只修干净', 'natural': '新郎不化妆，按婚纱照成片精修', 'refined': '新郎化淡淡的男士妆'}
    for bride_level in MAKEUP_LEVELS:
        for groom_level in MAKEUP_LEVELS:
            makeup = Makeup(bride_level, groom_level)
            prompts = (
                compose_preview_prompt('korean_minimal', '3:4', makeup=makeup),
                compose_moment_prompt('korean_minimal', EXAMPLE_SHOT, '3:4', scene=1, makeup=makeup, with_preview=True,
                                      side_references=[('bride', 'left')]),
                compose_identity_boost_moment_prompt('korean_minimal', EXAMPLE_SHOT, '3:4', scene=1, makeup=makeup),
            )
            for prompt in prompts:
                identity = prompt.split('\n\n')[1]
                assert allowed + '妆容按本提示词重新绘制，精修按婚纱照成片处理，不复制参考图的妆容和皮肤状态：' + bride[bride_level] in identity
                assert '；' + groom[groom_level] in identity
                # Skin tone, hairline and face width are retouched by level; changing the features stays forbidden.
                assert '脸的基本轮廓、五官比例、眼型、鼻型、唇形和年龄感都与' in identity and '肤色、发际线' not in identity
                assert '不要改变五官。' in identity and '瘦脸、磨皮' not in identity and '不要美颜' not in identity
                assert '新郎的发际线位置与参考照片一致' in identity and '不做全脸模糊、塑料感的磨皮' in identity
                assert '眼影、腮红和唇色的颜色按「服装与风格」里的妆容方向' in identity
                assert '不改变真实的身体比例。' in identity
                assert has_makeup(prompt)
                assert len(prompt.split('\n\n')) == 6
    # Levels whiten and clean the skin; none of them reshapes eyes, brows or nose (V3.41).
    for level in MAKEUP_LEVELS:
        identity = compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup(level, level)).split('\n\n')[1]
        looks = identity.split('不复制参考图的妆容和皮肤状态：')[1].split('两人的精修和妆容都按')[0]
        assert not any(term in looks for term in ('卧蚕', '眼线', '修容', '鼻影', '高光', '眉形'))
        assert '不画卧蚕，不拉长或加粗眼线，不通过修容改变脸部立体结构' in identity
        assert '不抬高、拉长或收窄鼻梁，不填充、上移或扩大苹果肌' in identity
        # Clean but real skin, identifying marks kept (V3.42).
        assert '毛孔细小但仍可见' in identity and '明显的痣、胎记和疤痕是本人的特征，保留不去' in identity
        assert '看不到' not in looks and '一丝不乱' not in looks
        assert '不是整齐生硬的贴片边缘' in identity
        assert '妆面的质感跟着这张照片的光线' in identity
        # Both follow the style's hairstyle at the length the photos show; only the groom's hairline stays locked.
        assert '两人的发型都按「服装与风格」里写的造型重新打理，不照搬参考照片里的发型样式' in identity
        assert '凌乱、翘起、压塌、油腻和毛躁的状态都不保留' in identity
        assert '两人头发的长短和参考照片一致，不凭空加长或剪短' in identity
        assert '新郎的发际线位置与参考照片一致' in identity
        assert not any(lock in identity for lock in ('以参考照片中可见的发长为上限', '原有发量', '额头露出程度接近参考照片', '头发的长短大致不变'))
    # Nose shadow, cheek shine and eye sparkle: bride only, from the standard level up; never the groom (V3.42).
    for level in MAKEUP_LEVELS:
        identity = compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup(level, level)).split('\n\n')[1]
        groom_text = identity.split('；新郎')[1].split('两人的精修和妆容都按')[0]
        assert not any(term in groom_text for term in ('鼻梁', '苹果肌', '眼神光'))
        bride_text = identity.split('不复制参考图的妆容和皮肤状态：')[1].split('；新郎')[0]
        assert (('鼻梁' in bride_text) and ('苹果肌' in bride_text) and ('眼神光' in bride_text)) == (level != 'light')
    groom_hair = {'light': '头发按「服装与风格」里的发型作基础整理', 'natural': '头发按「服装与风格」里的发型完成婚纱照造型',
                  'refined': '头发在「服装与风格」里的发型上加强发束层次和定型'}
    for level, text in groom_hair.items():
        assert text in compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup('light', level)).split('\n\n')[1]
    # Only the stronger levels slim the face, and only slightly.
    assert '脸型不做修饰' in compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup('light', 'light')).split('\n\n')[1]
    assert '下颌线和脸颊稍微收窄' in compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup('refined', 'light')).split('\n\n')[1]
    # Any prompt from V3.37 on counts as made up, whatever wording its makeup sentence had.
    v3_38 = compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup('refined', 'natural')).replace(
        '妆容按本提示词重新绘制，精修按婚纱照成片处理，不复制参考图的妆容和皮肤状态：', '妆容按本提示词重新绘制，不复制参考图的妆容浓度、眼线、睫毛或唇色：')
    assert has_makeup(v3_38)
    # Prompts before V3.37 say nothing about makeup and stay that way.
    legacy = compose_preview_prompt('korean_minimal', '3:4')
    assert '妆容按本提示词' not in legacy and not has_makeup(legacy)
    assert '不要美颜、瘦脸、磨皮或改变五官。' in legacy and '唇形、肤色、发际线和年龄感' in legacy


def test_hairstyle_follows_the_stated_hair_length_after_the_costume():
    style = STYLE_BY_ID['republic_qipao']
    for bride in BRIDE_HAIR_LENGTHS:
        for groom in GROOM_HAIR_LENGTHS:
            paragraph = compose_preview_prompt('republic_qipao', '3:4', makeup=Makeup('natural', 'natural'),
                                               hair=Hair(bride, groom)).split('\n\n')[2]
            assert paragraph.startswith('服装与风格：' + style['costume'] + '新娘发型（')
            assert style['hair']['bride'][bride] + '。新郎发型（' in paragraph
            assert style['hair']['groom'][groom] + '。' + style['look'] in paragraph
    assert '新娘发型（及肩）：民国手推波纹' in compose_preview_prompt('republic_qipao', '3:4', hair=Hair('medium', 'buzz'))
    assert '新郎发型（寸头）：' + BUZZ_HAIR in compose_preview_prompt('republic_qipao', '3:4', hair=Hair('medium', 'buzz'))
    # Hanfu keeps its Song-dynasty headwear instead of a modern buzz cut.
    assert '幞头' in STYLE_BY_ID['hanfu']['hair']['groom']['buzz']
    # Prompts without a stated length (before V3.45) name no hairstyle.
    assert '发型（' not in compose_preview_prompt('republic_qipao', '3:4')


def test_hair_prompts_omit_removed_hairline_density_and_avoid_rules():
    from app.retouch import retouch_preset

    for level in MAKEUP_LEVELS:
        bride = compose_bride_hair_prompt('low_bun')
        groom = compose_groom_hair_prompt('textured')
        assert len(bride.split('\n\n')) == len(groom.split('\n\n')) == 2
        assert '不再上妆、不再提白、不再修皮肤' in bride
        assert '已经修整好的眉毛' in groom
        prompts = [bride, groom, compose_bride_hair_prompt('low_bun', jewelry_id='pearl_studs')]
        for role, hairstyle_id, style_id in (('bride', 'low_bun', 'korean_minimal'), ('groom', 'textured', None)):
            for facing in (None, 'left', 'right'):
                for retouch_settings in (None, retouch_preset(level)):
                    prompts.append(compose_look_prompt(role, hairstyle_id, level, style_id=style_id,
                                                       facing=facing, retouch_settings=retouch_settings))
                    front = bride if role == 'bride' else groom
                    assert prompts[-1].split('\n\n')[1] == front.split('\n\n')[1]
        for prompt in prompts:
            assert '头发部分完全重新画' in prompt and '改变头发不授权改变脸部、耳朵或颅骨' in prompt
            assert '毛发质感：' in prompt.split('\n\n')[1]
            assert not any(term in prompt for term in (
                '真实发际线的主体位置', '局部稀疏', 'M 形', '头发不遮挡眼睛', '原照片里的毛躁',
                '没有任何翘起的细碎毛发', '不凭空加长头发', '发际线边缘用细软的绒发',
                '在原有头发生长区域内', '使视觉发量自然充盈', '分缝仍保留细窄', '分缝保持清楚、细窄',
                '避免：照搬原照片的头发状态',
                '头盔一样僵硬的头发', '生硬的贴片状发际线',
            ))
        assert '首饰：两只耳垂上各戴一颗小白色珍珠耳钉。' in prompts[2]
        assert '发际线的主体边界以这张照片为准' in bride and '发际线的主体边界以这张照片为准' in groom
    # The makeup step still leaves the hair alone.
    assert '发量' not in compose_bride_makeup_prompt('natural', 'korean_minimal')


def test_the_face_contour_follows_the_level_for_both_people():
    # V7.10: soft tissue only at the standard level, plus a very slight jawline at the refined one; light has none.
    from app.prompts import FACE_SHAPING_LEVELS, compose_bride_makeup_prompt, compose_look_prompt
    light, natural, refined = (compose_bride_makeup_prompt(level, 'korean_minimal') for level in ('light', 'natural', 'refined'))
    assert '脸部轮廓：' not in light
    assert '仅当照片中清楚可见下巴下方由松弛软组织形成的堆积时' in natural and '淡化鼻唇沟' in natural and '下颌两侧' not in natural
    assert '极轻微地收窄下颌两侧软组织的外轮廓' in refined and '不改变下巴形状、颧骨' in refined
    assert all('除本档位「脸部轮廓」部分明确允许的' in prompt for prompt in (light, natural, refined))
    assert '瘦脸' not in natural.split('不瘦脸')[-1] and 'V脸' not in refined
    # V7.15: the groom has the same contour at each level, without the bride's makeup.
    groom_light, groom_natural, groom_refined = (compose_look_prompt('groom', 'keep', level) for level in ('light', 'natural', 'refined'))
    assert '脸部轮廓：' not in groom_light
    assert '仅当照片中清楚可见下巴下方由松弛软组织形成的堆积时' in groom_natural and '下颌两侧' not in groom_natural and '新郎不化妆' in groom_natural
    assert '极轻微地收窄下颌两侧软组织的外轮廓' in groom_refined and '新郎化淡淡的男士妆' in groom_refined
    assert all('除本档位「脸部轮廓」部分明确允许的' in prompt and '骨骼结构与体积' in prompt for prompt in (groom_light, groom_natural, groom_refined))
    assert FACE_SHAPING_LEVELS == {'refined'}


def test_the_bride_wears_the_jewelry_picked_for_her_look_and_the_outfit_pictures_draw_none():
    """V7.61: earrings and necklace are an optional choice of a bride look, put on in her hair edit."""
    from app.prompts import (JEWELRY, JEWELRY_BY_ID, STYLE_JEWELRY_SUGGESTION, STYLE_OUTFITS, OUTFIT_BY_ID, RETIRED_STYLE_IDS,
                             compose_bride_hair_prompt, compose_look_prompt, style_art_product_prompt)
    hair = compose_bride_hair_prompt('low_bun', jewelry_id='pearl_drops_necklace')
    assert '并戴上下面写的首饰，这一步只改头发和戴首饰：' in hair
    assert hair.endswith('\n\n首饰：' + JEWELRY_BY_ID['pearl_drops_necklace'].text)
    plain = compose_bride_hair_prompt('low_bun')
    assert '首饰' not in plain and '这一步只改头发：' in plain
    side = compose_look_prompt('bride', 'low_bun', 'refined', style_id='lawn_floral', facing='left', jewelry_id='diamond_drops')
    assert '首饰：' + JEWELRY_BY_ID['diamond_drops'].text in side
    assert '首饰' not in compose_look_prompt('bride', 'low_bun', 'refined', style_id='lawn_floral', facing='left')
    for item in JEWELRY:
        assert item.text in DOC
        assert '首饰：' + item.text in compose_bride_hair_prompt('low_bun', jewelry_id=item.id)
        assert '首饰：' + item.text in compose_look_prompt(
            'bride', 'low_bun', 'refined', style_id='forest_film', facing='left', jewelry_id=item.id)
    assert len(JEWELRY_BY_ID) == len(JEWELRY)
    assert set(STYLE_JEWELRY_SUGGESTION) == set(STYLE_BY_ID) - RETIRED_STYLE_IDS
    for style_id, jewelry_id in STYLE_JEWELRY_SUGGESTION.items():
        assert jewelry_id in JEWELRY_BY_ID and style_id not in RETIRED_STYLE_IDS
    for style_id, outfits in STYLE_OUTFITS.items():
        if style_id in RETIRED_STYLE_IDS:
            continue
        for outfit_id in outfits:
            outfit = OUTFIT_BY_ID[outfit_id]
            assert not any(word in outfit.bride + outfit.headwear for word in ('耳钉', '耳坠', '耳环', '项链'))
            assert '不画耳环、耳坠等耳饰，也不画项链。' in style_art_product_prompt(outfit_id)


def test_a_bride_look_keeps_its_jewelry_and_none_is_the_default(app_factory):
    from conftest import create_project
    with app_factory() as (client, app):
        project = create_project(client)
        options = client.get('/api/look-options').json()
        assert [item['id'] for item in options['jewelry']] == [
            'pearl_studs', 'pearl_drops', 'pearl_drops_necklace', 'diamond_drops', 'diamond_studs',
            'pearl_pendant_set', 'pearl_flower_studs', 'gold_leaf_drops', 'red_agate_drops', 'shell_pearl_drops',
            'pearl_choker_set', 'diamond_necklace_set', 'jade_drops', 'diamond_tassel_drops', 'pearl_diamond_clusters',
        ]
        assert options['style_jewelry']['french_couture'] == 'pearl_drops_necklace'
        active_styles = {style['id'] for style in client.get('/api/styles').json() if not style['retired']}
        assert set(options['style_jewelry']) == active_styles
        assert set(options['style_jewelry'].values()) <= {item['id'] for item in options['jewelry']}
        with app.state.db.connect() as conn:
            assert 'jewelry_id' in [row[1] for row in conn.execute('PRAGMA table_info(looks)')]


def test_a_shot_names_jewelry_only_when_the_look_wears_some():
    """V7.64: shots from looks without jewelry came out with earrings."""
    bare = compose_preview_prompt('forest_film', '3:4', styled=True, outfit='forest_lace_halter', art=True, room='pine_forest')
    worn = compose_preview_prompt('forest_film', '3:4', styled=True, outfit='forest_lace_halter', art=True, room='pine_forest',
                                  jewelry=True)
    assert '耳饰和项链参考' not in bare and '新娘不戴耳饰和项链，和造型照一致；' in bare and '以造型照里的为准' not in bare
    assert '新娘的耳饰和项链参考。' in worn and '新娘的耳饰和项链以造型照里的为准，戴法和款式一致；' in worn and '不戴耳饰' not in worn
    assert '新娘不戴耳饰和项链，和造型照一致；' in DOC


def test_the_groom_retouch_cleans_stubble_tired_eyes_and_complexion_from_natural_up():
    """V7.87: the controls only whiten and narrow; natural and refined also clean what makes a groom look tired."""
    from app.prompts import compose_groom_retouch_prompt
    from app.retouch import retouch_preset
    for level in ('natural', 'refined'):
        prompt = compose_groom_retouch_prompt(level, retouch_preset(level))
        assert '胡茬刮干净' in prompt and '减轻眼袋、眼皮浮肿和疲态' in prompt and '不改变眼型、眼睛大小和双眼皮' in prompt
        assert '唇色自然红润' not in prompt and '不显得涂了口红' in prompt
        # V7.88: his brows are filled inside their own outline, and "no makeup" no longer contradicts that.
        assert '用比本人眉色略深一点的颜色' in prompt and '不补画' not in prompt  # V7.90
        assert '妆感' in prompt and '底妆：轻薄底妆统一肤色' in prompt and '淡淡的男士妆' not in prompt
        # V7.91: the step includes makeup, the brow is not thinned and does not fade with the whitening.
        assert '这一步只修整皮肤、化妆和画眉' in prompt and '不清理、不修剪眉毛' in prompt and '只清理' not in prompt
        assert '眉毛不跟着变浅' in prompt and '和原照片相比明显更浓密' in prompt
        assert '新郎不化妆' not in prompt
        assert '哑光定妆：额头、鼻子和两颊不反油光' in prompt  # V7.89
    light = compose_groom_retouch_prompt('light', retouch_preset('light'))
    assert '胡茬' not in light and '不补画、不加浓' in light and '哑光定妆' not in light
    assert '这一步只修整皮肤、按下面的档位精修' in light
    # Older candidates without fine settings keep their own words.
    assert '胡茬' not in compose_groom_retouch_prompt('refined')


def test_new_bride_makeups_set_the_base_so_the_t_zone_does_not_shine():
    """V7.92: 通透、柔缎、微润 came out as an oily forehead and nose on the look, which the shots then keep."""
    from app.prompts import compose_bride_makeup_prompt
    from app.retouch import retouch_preset
    for level in ('light', 'natural', 'refined'):
        for style_id in ('chinese_celebration', 'city_story', 'french_vintage'):
            prompt = compose_bride_makeup_prompt(level, style_id, retouch_preset(level))
            finish, setting = prompt.split('新娘妆面：')[1].split('定妆：')[:2]
            assert finish.endswith('。') and setting.startswith('额头、鼻子、下巴和两颊不反油光')
    # Older candidates without fine settings keep their own words.
    assert '定妆：' not in compose_bride_makeup_prompt('refined', 'chinese_celebration')
