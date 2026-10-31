"""Prompt composition contract for V3 prompts."""

import re
from pathlib import Path

import pytest

from app.prompts import (
    GENERAL_AVOID,
    POSES,
    QUALITY_PARAGRAPH,
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
    Makeup,
    compose_preview_prompt,
    compose_look_prompt,
    HAIRSTYLES,
    MAKEUP_COLORS,
    STYLE_MAKEUP_COLORS,
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
    assert compose_moment_prompt('cinematic_light', expected.split('\n\n')[3].removeprefix('镜头：'), '3:4', scene=2, styled=True) == expected


def test_styled_shot_prompts_copy_the_styled_photos_and_match_docs():
    identity = compose_preview_prompt('korean_minimal', '3:4', styled=True).split('\n\n')[1]
    assert identity == doc_block('**V4.0 起：参考图是两人的造型照')
    assert is_styled(compose_preview_prompt('korean_minimal', '3:4', styled=True))
    assert not is_styled(compose_preview_prompt('korean_minimal', '3:4', makeup=Makeup('natural', 'natural'), hair=Hair('long', 'short')))
    for style in STYLES:
        prompt = compose_moment_prompt(style['id'], EXAMPLE_SHOT, '3:4', scene=1, styled=True)
        assert_six_paragraphs(prompt, '镜头：')
        # Hair and makeup are on the styled photos: the style paragraph names neither (V4.0); the updo styles add
        # their headwear on top of the look's hairstyle (V5.0).
        paragraph = prompt.split('\n\n')[2]
        clothes = style['costume'].split('妆容方向：')[0]
        assert '妆容方向' not in paragraph and '发型：' not in paragraph
        assert paragraph.startswith('服装与风格：' + clothes + UPDO_STYLES.get(style['id'], '') + style['look'])
        assert '新娘化' not in prompt and not has_makeup(prompt)
    sides = compose_moment_prompt('korean_minimal', EXAMPLE_SHOT, '3:4', styled=True, with_preview=True,
                                  side_references=[('bride', 'left'), ('groom', 'right')]).split('\n\n')[1]
    assert sides.startswith('图片角色：第一张图是新娘的造型照，第二张图是新郎的造型照，第三张图是新娘脸朝画面左侧时的造型照，'
                            '第四张图是新郎脸朝画面右侧时的造型照。第五张图是风格参考')
    boosted = compose_identity_boost_moment_prompt('korean_minimal', EXAMPLE_SHOT, '3:4', scene=1, styled=True)
    assert is_styled(boosted) and boosted.split('\n\n')[1].endswith('宁可画面朴素，也要像本人。')


def test_look_prompts_cover_every_hairstyle_level_and_colour_and_match_docs():
    assert doc_block('新娘正面照，`low_bun`') == compose_look_prompt('bride', 'low_bun', 'natural', 'classic_red')
    assert doc_block('新郎脸朝画面左侧，`keep`') == compose_look_prompt('groom', 'keep', 'natural', facing='left')
    for hairstyle in HAIRSTYLES:
        assert f"| `{hairstyle.id}` {hairstyle.name} | {'、'.join(f'`{length}`' for length in hairstyle.lengths)} | " \
               f"{'是' if hairstyle.updo else '否'} | `{hairstyle.text}` |" in DOC
        for level in MAKEUP_LEVELS:
            for color in (MAKEUP_COLORS if hairstyle.role == 'bride' else (None,)):
                prompt = compose_look_prompt(hairstyle.role, hairstyle.id, level, color.id if color else None)
                paragraphs = prompt.split('\n\n')
                assert len(paragraphs) == 5
                assert paragraphs[1].startswith('发型：' + hairstyle.text + '；')
                assert ('妆容颜色：' + color.text in prompt) if color else '妆容颜色' not in prompt
                # Looks do not reshape the face (V4.0, first version).
                assert not any(term in prompt for term in ('瘦脸', '收窄', '补低', '鼻梁', '苹果肌'))
                assert paragraphs[2].split('。')[0].removeprefix('皮肤和妆容：') in DOC
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
