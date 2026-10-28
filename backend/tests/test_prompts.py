"""Prompt composition contract for V3 prompts."""

import pytest

from app.prompts import (
    GENERAL_AVOID,
    POSES,
    QUALITY_PARAGRAPH,
    SAME_LOOK,
    STYLES,
    WIDE_QUALITY_PARAGRAPH,
    compose_identity_boost_moment_prompt,
    compose_moment_prompt,
    compose_photo_prompt,
    compose_preview_prompt,
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


def test_full_example_matches_docs_verbatim():
    expected = '\n\n'.join((
        '竖构图，3:4 比例的写实婚纱照。',
        IDENTITY_TEXT,
        '服装与风格：新娘：保持参考照片中的发际线和分缝，光洁的低盘发，没有碎发；干净的底妆，大地色眼妆，裸色唇妆；简洁的钻石耳钉，不戴头纱；极简缎面婚纱，一字肩、收腰，没有任何蕾丝和装饰，缎面在光下有柔和的光泽，裙摆垂顺及地。新郎：保持参考照片中的发型轮廓和发际线，打理整齐；黑色戗驳领礼服，白衬衫，黑色领结。同一组照片里，两人的服装、发型、妆容和配饰保持完全一致。光和影是画面的主角：方向明确的光在墙面、地面和人物身上形成清晰的明暗结构或投影图案；背景简洁，颜色服务于光影；灯具等光源本身不出现在画面里。',
        '镜头：' + EXAMPLE_SHOT,
        '画面质感：真实相机拍摄的婚纱摄影作品，电影感，极致细节，高级质感。人物与环境处在同一光源下，受光方向、色温和明暗一致，'
        '人物脚下或身后有自然的接触阴影；两人是画面的主体，环境、光影和留白只是衬托；两人面部曝光充足、清晰；'
        '表情自然、真实，符合这个瞬间的情绪；'
        '焦外自然虚化，色彩处理统一，皮肤保留真实质感。',
        '避免：网红脸、整容脸、幼态脸、五官欧美化、男模脸、偶像剧男主脸、下颌被削尖、脸型被拉长、过度磨皮的塑料皮肤、面部被遮挡、多余的人物、手部或手指畸形、'
        '人物像抠图贴在背景上、影楼廉价感、叠加在画面上的标题文字、水印、乱码文字、脸部大面积阴影、半边脸死黑、条纹或树影横跨眼睛、剪影、过度高反差。',
    ))
    assert compose_moment_prompt('cinematic_light', EXAMPLE_SHOT, '3:4') == expected


def test_preview_and_legacy_poses_use_the_first_variant_but_director_shots_do_not():
    for style in STYLES:
        scene = '场景：' + style['variants'][0][1]
        assert compose_preview_prompt(style['id'], '3:4').split('\n\n')[2] == style['scene_prompt'] + scene
        assert compose_photo_prompt(style['id'], 'pose_01', '3:4').split('\n\n')[2] == style['scene_prompt'] + scene
        assert compose_moment_prompt(style['id'], EXAMPLE_SHOT, '3:4').split('\n\n')[2] == style['scene_prompt']


def test_preview_reference_is_inserted_after_groom_sentence_and_keeps_identity_rules():
    identity = compose_photo_prompt(
        'korean_minimal', 'pose_01', '3:4', with_preview=True,
    ).split('\n\n')[1]
    marker = '第二张图是新郎的身份参考。'
    assert identity == IDENTITY_TEXT.replace(marker, marker + PREVIEW_INSERTION, 1)
    assert identity.index(PREVIEW_INSERTION) == identity.index(marker) + len(marker)
    assert identity.replace(PREVIEW_INSERTION, '') == IDENTITY_TEXT


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


def test_wide_shots_tell_the_model_not_to_enlarge_people():
    paragraphs = compose_moment_prompt('hongkong_retro', '两人站在街口。', '4:3', shot_size='wide').split('\n\n')
    assert paragraphs[4] == WIDE_QUALITY_PARAGRAPH
    assert '不要为了看清面部而放大人物' in paragraphs[4] and '两人面部曝光充足' not in paragraphs[4]


def test_avoid_terms_allow_signage_but_not_overlays_or_garbled_text():
    assert '文字和水印' not in GENERAL_AVOID
    assert all(term in GENERAL_AVOID for term in ('叠加在画面上的标题文字', '水印', '乱码文字'))


def test_every_style_asks_for_one_look_across_the_set():
    for style in STYLES:
        paragraph = compose_moment_prompt(style['id'], '两人相视而笑。', '3:4').split('\n\n')[2]
        assert paragraph == '服装与风格：' + style['costume'] + SAME_LOOK + style['look']
