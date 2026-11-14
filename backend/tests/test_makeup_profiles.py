"""Model-aware makeup prompts, independent controls and immutable task snapshots."""

import re
from typing import get_args

import pytest

from app.generator import GeneratorError, OutcomeKind
from app.models import MODEL_REGISTRY
from app.prompts import (MAKEUP_LEVELS, compose_bride_hair_prompt, compose_bride_makeup_prompt,
                         compose_groom_hair_prompt, compose_groom_retouch_prompt, compose_look_prompt)
from app.retouch import AdjustmentLevel, ConcealingLevel, RetouchSettings, retouch_preset
from conftest import (PNG, ScriptedGenerator, create_director_photo_batch, create_project,
                      preview_batch, wait_batch, wait_until)
from test_makeups import make_bride_looks, make_makeups, makeup_state
from test_styling import preview_slot


NANO = 'gemini-nano-banana-2.1'
PRO = 'gemini-3-pro-image'
SEEDREAM = 'doubao-seedream-5-5-261028'
GENTLE_MODELS = (NANO, PRO, SEEDREAM)
STRONG_WORDING = ('妆色鲜明', '充分着色', '饱满着色', '完整覆盖', '浓郁鲜明',
                  '明显更浓密', '颜色更深', '白一到两个色号', '影楼款假睫毛')
SEEDREAM_CONTROL_LABELS = {'brightening': '提亮', 'concealing': '遮瑕', 'slimming': '下颌两侧软组织收整',
                           'chin': '下巴下方软组织收整', 'nasolabial': '鼻唇沟淡化'}
SEEDREAM_REMOVED_WORDING = ('眼影', '腮红', '睫毛', '薄涂', '胡茬', '男士淡妆',
                            '精致淡妆', '轻微范围上限', '婚礼当天', '化好妆', '轻妆人像编辑')


@pytest.mark.parametrize('model_id', GENTLE_MODELS)
@pytest.mark.parametrize('level', MAKEUP_LEVELS)
def test_gentle_makeup_is_short_and_contains_no_emphatic_default_instructions(model_id, level):
    settings = retouch_preset(level)
    bride = compose_bride_makeup_prompt(level, 'city_story', settings, model_id=model_id)
    groom = compose_groom_retouch_prompt(level, settings, model_id=model_id)
    for prompt in (bride, groom):
        assert len(prompt) < (200 if model_id == SEEDREAM else 400)
        assert not any(wording in prompt for wording in STRONG_WORDING)
        assert '头发和发色保持原样' in prompt and '五官结构、脸型特征、眉形' in prompt
        assert '肤色底调和皮肤纹理' in prompt
        assert '浅灰色' in prompt and '衣服保持原样' in prompt
        if model_id == SEEDREAM:
            assert not any(wording in prompt for wording in SEEDREAM_REMOVED_WORDING)
            assert '其余照原片' in prompt
        else:
            assert '关闭项保持原样' in prompt
    if model_id == SEEDREAM:
        assert '只参考玫瑰豆沙的色相' in bride
        if level == 'refined':
            assert '至多添一丝毛流' in groom and '原有唇色不变' in groom
        else:
            assert '空缺' not in groom
        if level == 'natural':
            assert '只添一丝妆色' in bride and '原眉照原样' in groom
        return
    assert '妆色参考玫瑰豆沙，薄涂透肤' in bride
    if level == 'light':
        assert '化妆' not in groom.split('\n\n')[0] and '轻妆' not in groom.split('\n\n')[0]
        assert '补空缺' not in groom
    else:
        assert '空缺' in groom and '唇色' in groom


def test_nano_banana_and_seedream_have_distinct_gentle_wording():
    nano = compose_bride_makeup_prompt('natural', 'city_story', model_id=NANO)
    seedream = compose_bride_makeup_prompt('natural', 'city_story', model_id=SEEDREAM)
    assert nano != seedream
    assert compose_bride_makeup_prompt('natural', 'city_story', model_id=PRO) == nano
    assert len({compose_bride_makeup_prompt(level, 'city_story', model_id=NANO) for level in MAKEUP_LEVELS}) == 3


@pytest.mark.parametrize('model_id', GENTLE_MODELS)
@pytest.mark.parametrize('field,label', [('brightening', '肤色提亮'), ('concealing', '遮瑕程度'),
                                         ('slimming', '瘦脸程度'), ('chin', '下巴轮廓整理'),
                                         ('nasolabial', '法令纹淡化')])
def test_gentle_controls_keep_all_eleven_positions_distinct_and_do_not_change_other_controls(model_id, field, label):
    values = get_args(ConcealingLevel if field == 'concealing' else AdjustmentLevel)
    instructions = []
    others = set()
    for value in values:
        settings = RetouchSettings.model_validate(retouch_preset('natural').model_dump() | {field: value})
        prompt = compose_bride_makeup_prompt('refined', 'city_story', settings, model_id=model_id)
        if model_id == SEEDREAM:
            active = re.search('仅调整：([^。]+)', prompt)
            assert active is not None
            controls = dict(re.findall('([^：；]+)：([^；]+)', active.group(1)))
            assert set(controls) == {control_label for control_field, control_label in SEEDREAM_CONTROL_LABELS.items()
                                     if getattr(settings, control_field) != 'off'}
            instructions.append(controls.pop(SEEDREAM_CONTROL_LABELS[field], None))
            others.add(tuple(controls.items()))
            continue
        match = re.search(re.escape(label) + '：([^；。]+)', prompt)
        assert match is not None and prompt.count(label + '：') == 1
        instructions.append(match.group(1))
        others.add(prompt[:match.start(1)] + prompt[match.end(1):])
    assert len(set(instructions)) == 11
    assert len(others) == 1


@pytest.mark.parametrize('model_id', GENTLE_MODELS)
def test_disabled_controls_stay_disabled_even_at_the_refined_makeup_level(model_id):
    settings = RetouchSettings(slimming='off', brightening='off', concealing='off', chin='off', nasolabial='off')
    for prompt in (compose_bride_makeup_prompt('refined', 'city_story', settings, model_id=model_id),
                   compose_groom_retouch_prompt('refined', settings, model_id=model_id)):
        if model_id == SEEDREAM:
            assert '原有肤色、瑕疵和轮廓照原片，保留皮肤纹理' in prompt
            assert '仅调整：' not in prompt
            assert not any(label + '：' in prompt for label in SEEDREAM_CONTROL_LABELS.values())
            continue
        assert '肤色提亮：保持原肤色明度，不提白' in prompt
        assert '遮瑕程度：保留原有瑕疵' in prompt
        assert '瘦脸程度：关闭，不瘦脸' in prompt
        assert '下巴轮廓整理：关闭，保留原有轮廓' in prompt
        assert '法令纹淡化：保留' in prompt


@pytest.mark.parametrize('model_id', [model.id for model in MODEL_REGISTRY if model.family == 'openai'])
def test_gpt_makeup_and_side_prompts_keep_the_default_wording(model_id):
    for level in MAKEUP_LEVELS:
        for settings in (None, retouch_preset(level)):
            assert compose_bride_makeup_prompt(level, 'city_story', settings, model_id=model_id) == \
                   compose_bride_makeup_prompt(level, 'city_story', settings)
            assert compose_groom_retouch_prompt(level, settings, model_id=model_id) == compose_groom_retouch_prompt(level, settings)
            kwargs = dict(style_id='city_story', facing='left', retouch_settings=settings)
            assert compose_look_prompt('bride', 'low_bun', level, model_id=model_id, **kwargs) == \
                   compose_look_prompt('bride', 'low_bun', level, **kwargs)


@pytest.mark.parametrize('model_id', GENTLE_MODELS)
def test_side_makeup_uses_the_model_profile_without_changing_hair_or_jewelry(model_id):
    for level in MAKEUP_LEVELS:
        for facing in ('left', 'right'):
            for role, hairstyle, style_id in (('bride', 'low_bun', 'city_story'), ('groom', 'textured', None)):
                jewelry_id = 'pearl_studs' if role == 'bride' else None
                prompt = compose_look_prompt(role, hairstyle, level, style_id=style_id, facing=facing,
                                             jewelry_id=jewelry_id, retouch_settings=retouch_preset(level), model_id=model_id)
                assert not any(wording in prompt for wording in STRONG_WORDING)
                hair = compose_bride_hair_prompt(hairstyle) if role == 'bride' else compose_groom_hair_prompt(hairstyle)
                assert prompt.split('\n\n')[1] == hair.split('\n\n')[1]
                assert ('首饰：' in prompt) == (role == 'bride')
                if model_id == SEEDREAM:
                    assert not any(wording in prompt for wording in SEEDREAM_REMOVED_WORDING)
                    assert '一点点局部修饰' in prompt.split('\n\n')[0]


@pytest.mark.parametrize('model_id', GENTLE_MODELS)
@pytest.mark.parametrize('person', ['a', 'b'])
def test_makeup_snapshot_and_retry_keep_the_model_profile_after_settings_change(app_factory, model_id, person):
    stylist = ScriptedGenerator([GeneratorError(OutcomeKind.REJECTED)])
    with app_factory(stylist=stylist) as (client, app):
        provider = 'ark' if model_id == SEEDREAM else '88api'
        response = client.patch('/api/settings', json={'makeup_provider': provider, 'makeup_model': model_id})
        assert response.status_code == 200, response.text
        project = create_project(client)
        body = {'client_request_id': 'profile', 'person': person, 'count': 1}
        if person == 'a':
            body['style_id'] = 'city_story'
        response = client.post(f"/api/projects/{project['id']}/makeups", json=body)
        assert response.status_code == 201, response.text
        makeup_id = response.json()[0]['id']
        wait_until(lambda: makeup_state(client, project['id'])[makeup_id], lambda row: row['status'] == 'failed')
        expected = (compose_bride_makeup_prompt('natural', 'city_story', retouch_preset('natural'), model_id=model_id)
                    if person == 'a' else compose_groom_retouch_prompt('natural', retouch_preset('natural'), model_id=model_id))
        with app.state.db.connect() as conn:
            row = conn.execute('SELECT * FROM makeups WHERE id=?', (makeup_id,)).fetchone()
            assert (row['model'], row['prompt']) == (model_id, expected)
        assert stylist.calls[0][1] == expected
        response = client.patch('/api/settings', json={'makeup_provider': '88api', 'makeup_model': 'gpt-image-2'})
        assert response.status_code == 200, response.text
        assert client.post(f'/api/makeups/{makeup_id}/retry').status_code == 200
        wait_until(lambda: makeup_state(client, project['id'])[makeup_id], lambda row: row['status'] == 'ready')
        assert stylist.models == [model_id, model_id]
        assert [call[1] for call in stylist.calls] == [expected, expected]


@pytest.mark.parametrize('model_id', GENTLE_MODELS)
def test_manual_side_looks_follow_the_look_stage_model_instead_of_the_makeup_model(app_factory, model_id):
    with app_factory() as (client, app):
        project = create_project(client)
        make_makeups(client, project['id'], 1)
        [look_id] = make_bride_looks(client, project['id'], ['low_bun'])
        client.post(f"/api/projects/{project['id']}/images", data={'person': 'a', 'angle': 'left'},
                    files={'file': ('left.png', PNG, 'image/png')})
        provider = 'ark' if model_id == SEEDREAM else '88api'
        response = client.patch('/api/settings', json={'look_provider': provider, 'look_model': model_id})
        assert response.status_code == 200, response.text
        assert client.post(f'/api/looks/{look_id}/side-looks').status_code == 201
        sides = wait_until(lambda: client.get(f"/api/projects/{project['id']}").json()['side_looks'],
                           lambda rows: len(rows) == 1 and rows[0]['status'] == 'ready')
        expected = compose_look_prompt('bride', 'low_bun', 'natural', style_id='city_story', facing='left',
                                       retouch_settings=retouch_preset('natural'), model_id=model_id)
        assert app.state.stylist.models[-1] == model_id and app.state.stylist.calls[-1][1] == expected
        with app.state.db.connect() as conn:
            row = conn.execute('SELECT * FROM looks WHERE id=?', (sides[0]['id'],)).fetchone()
            assert (row['model'], row['prompt']) == (model_id, expected)


@pytest.mark.parametrize('model_id', [NANO, SEEDREAM])
def test_automatic_side_looks_use_the_gentle_profile(app_factory, model_id):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        client.post(f"/api/projects/{project['id']}/images", data={'person': 'b', 'angle': 'left'},
                    files={'file': ('left.png', PNG, 'image/png')})
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
        provider = 'ark' if model_id == SEEDREAM else '88api'
        response = client.patch('/api/settings', json={'look_provider': provider, 'look_model': model_id})
        assert response.status_code == 200, response.text
        sample = preview_slot(client, project['id'])
        response = create_director_photo_batch(client, project['id'], sample['id'], request_id='gentle-side')
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        calls = [call for call in app.state.stylist.calls if '脸朝画面左侧' in call[1]]
        assert len(calls) == 1
        expected = compose_look_prompt('groom', 'keep', 'natural', facing='left',
                                       retouch_settings=retouch_preset('natural'), model_id=model_id)
        assert calls[0][1] == expected


def test_unknown_model_is_rejected_instead_of_silently_using_default_strength():
    with pytest.raises(ValueError, match='不支持模型'):
        compose_bride_makeup_prompt('natural', 'city_story', model_id='unknown')
