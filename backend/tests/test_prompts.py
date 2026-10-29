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


DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()


def doc_block(heading: str) -> str:
    """First ```text block after a docs/prompts.md heading."""
    return re.search(re.escape(heading) + r'.*?```text\n(.*?)\n```', DOC, re.S).group(1)


def test_full_example_matches_docs_verbatim():
    expected = doc_block('## 5. 完整示例')
    assert compose_moment_prompt('cinematic_light', expected.split('\n\n')[3].removeprefix('镜头：'), '3:4') == expected


def test_shared_paragraphs_and_costumes_match_docs_verbatim():
    assert doc_block('### 2.5 画面质感') == QUALITY_PARAGRAPH
    assert doc_block('通用避免项：') == GENERAL_AVOID
    for style in STYLES:
        assert doc_block(f" `{style['id']}`\n") == style['costume']


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
