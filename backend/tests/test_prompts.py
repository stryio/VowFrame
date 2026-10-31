"""Prompt composition contract for V3 prompts."""

import re
from pathlib import Path

import pytest

from app.prompts import (
    GENERAL_AVOID,
    POSES,
    QUALITY_PARAGRAPH,
    STYLES,
    compose_identity_boost_moment_prompt,
    compose_moment_prompt,
    compose_photo_prompt,
    MAKEUP_LEVELS,
    Makeup,
    compose_preview_prompt,
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
    assert compose_moment_prompt('cinematic_light', expected.split('\n\n')[3].removeprefix('镜头：'), '3:4', scene=2, makeup=Makeup('natural', 'natural')) == expected


def test_shared_paragraphs_and_costumes_match_docs_verbatim():
    assert doc_block('### 2.5 画面质感') == QUALITY_PARAGRAPH
    assert doc_block('通用避免项：') == GENERAL_AVOID
    for style in STYLES:
        assert doc_block(f" `{style['id']}`\n") == style['costume']
        # Strength comes from the project level; a style only sets the hair and the makeup colors (V3.39).
        assert '淡妆' not in style['costume'] and '妆容方向：' in style['costume']
        # A style names the bride's hairstyle only; tidying both heads of hair is retouching, set by level (V3.41).
        assert '新娘发型：' in style['costume'] and '分缝' not in style['costume']
        assert '新郎的头发' not in style['costume'] and '参考照片' not in style['costume']
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
    assert compose_preview_prompt('chinese_traditional', '3:4').startswith('竖构图，3:4 比例的写实婚纱照。')
    assert compose_preview_prompt('chinese_traditional', '4:3').startswith('横构图，4:3 比例的写实婚纱照。')
    assert compose_preview_prompt('chinese_traditional', '1:1').startswith('方构图，1:1 比例的写实婚纱照。')


@pytest.mark.parametrize('aspect_ratio', ['', '3/4', '0:3', '3:0', '3:4:1', '3:4 ', None])
def test_malformed_aspect_ratio_raises(aspect_ratio):
    with pytest.raises(ValueError):
        compose_preview_prompt('chinese_traditional', aspect_ratio)


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
        assert '面部被遮挡' in normal[5] and '面部被遮挡' not in atmosphere[5]
        assert '剪影' not in atmosphere[5].replace('、', ' ').split()
        assert atmosphere[:4] == normal[:4]
        assert is_faceless_shot('\n\n'.join(atmosphere)) and not is_faceless_shot('\n\n'.join(normal))
    # Atmosphere shots stored with an older quality paragraph are still recognised.
    older = '\n\n'.join(atmosphere).replace('皮肤干净细腻、有自然的光泽。', '皮肤保留真实质感。')
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
                assert '新郎的发际线与参考照片一致' in identity and '不是塑料感的磨皮' in identity
                assert '眼影、腮红和唇色的颜色按「服装与风格」里的妆容方向' in identity
                assert '不改变真实的身体比例。' in identity
                assert has_makeup(prompt)
                assert len(prompt.split('\n\n')) == 6
    # Levels whiten and clean the skin; none of them reshapes eyes, brows or nose (V3.41).
    for level in MAKEUP_LEVELS:
        identity = compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup(level, level)).split('\n\n')[1]
        looks = identity.split('不复制参考图的妆容和皮肤状态：')[1].split('两人的精修和妆容都按')[0]
        assert not any(term in looks for term in ('卧蚕', '眼线', '修容', '鼻影', '高光', '眉形'))
        assert '不画卧蚕，不拉长或加粗眼线，不做鼻影和改变脸部立体结构的修容' in identity
        assert '妆面的质感跟着这张照片的光线' in identity
        # Hair is not likeness, so a messy reference loses to the level's grooming.
        assert '头发不算长相' in identity and '凌乱、翘起、压塌、油腻和毛躁的状态都不保留' in identity
        assert '新郎的发际线与参考照片一致，头发的长短大致不变' in identity
    groom_hair = {'light': '头发梳理整齐，没有乱翘的发丝', 'natural': '头发修剪打理成干净利落的发型',
                  'refined': '头发做精致的造型，用发胶定型'}
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
