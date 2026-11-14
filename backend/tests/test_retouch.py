"""Independent controls, immutable generation settings and compatibility with legacy candidates."""

import itertools
import json
import sqlite3
from typing import get_args

import pytest

from app.db import Database
from app.generator import GeneratorError
from app.prompts import compose_bride_makeup_prompt, compose_groom_retouch_prompt, compose_look_prompt
from app.retouch import AdjustmentLevel, ConcealingLevel, RetouchSettings, has_face_shaping, retouch_preset, stored_retouch_settings
from app.styling import auto_select_makeup
from conftest import PNG, ScriptedGenerator, create_director_photo_batch, create_project, preview_batch, wait_batch, wait_until
from test_makeups import make_bride_looks, make_makeups, makeup_state


def settings(**changes):
    return retouch_preset('natural').model_dump() | changes


def project_state(client, project_id):
    return client.get(f'/api/projects/{project_id}').json()


def test_controls_are_saved_independently_and_presets_reset_only_their_role(app_factory):
    with app_factory() as (client, app):
        project = create_project(client)
        url = f"/api/projects/{project['id']}"
        assert project['bride_retouch_settings'] == settings()
        assert project['groom_retouch_settings'] == settings()
        bride = settings(slimming='moderate', brightening='off', concealing='light', chin='off', nasolabial='off')
        groom = settings(slimming='light', concealing='full')
        response = client.patch(url, json={'bride_retouch_settings': bride, 'groom_retouch_settings': groom})
        assert response.status_code == 200
        assert response.json()['bride_retouch_settings'] == bride
        assert response.json()['groom_retouch_settings'] == groom
        # A hair-length or name change cannot silently reset the independent controls.
        updated = client.patch(url, json={'name': '精细妆容', 'bride_hair_length': 'medium'}).json()
        assert updated['bride_retouch_settings'] == bride
        assert updated['groom_retouch_settings'] == groom
        # Selecting even the same preset explicitly resets only that person's overrides.
        updated = client.patch(url, json={'makeup_level': 'natural'}).json()
        assert updated['bride_retouch_settings'] == settings()
        assert updated['groom_retouch_settings'] == groom
        # An explicit settings object in the same request takes precedence over preset defaults.
        updated = client.patch(url, json={'makeup_level': 'refined', 'bride_retouch_settings': bride}).json()
        assert updated['makeup_level'] == 'refined' and updated['bride_retouch_settings'] == bride
        app.state.db.initialize()
        assert project_state(client, project['id'])['bride_retouch_settings'] == bride


@pytest.mark.parametrize('invalid', [None, {}, {'slimming': 'light'}, settings(slimming=50),
                                    settings(slimming='unbounded'), settings(chin=None), settings(unknown='light')])
def test_invalid_or_incomplete_controls_are_rejected_without_modifying_project(client_app, invalid):
    client, app = client_app
    project = create_project(client)
    for field in ('bride_retouch_settings', 'groom_retouch_settings'):
        response = client.patch(f"/api/projects/{project['id']}", json={field: invalid})
        assert response.status_code == 422
        assert project_state(client, project['id'])[field] == settings()


def test_each_control_has_one_instruction_and_does_not_change_the_makeup_level():
    # Keep every legacy combination covered, including disabling contour edits on the strongest makeup preset.
    for slimming, brightening, concealing, chin, nasolabial in itertools.product(
            ('off', 'light', 'moderate'), ('off', 'light', 'moderate'), ('light', 'standard', 'full'),
            ('off', 'light', 'moderate'), ('off', 'light', 'moderate')):
        detail = RetouchSettings(slimming=slimming, brightening=brightening, concealing=concealing, chin=chin, nasolabial=nasolabial)
        for prompt in (compose_bride_makeup_prompt('refined', 'city_story', detail),
                       compose_groom_retouch_prompt('light', detail),
                       compose_look_prompt('bride', 'low_bun', 'refined', style_id='city_story', facing='left', retouch_settings=detail)):
            for label in ('瘦脸程度：', '肤色提亮：', '遮瑕程度：', '下巴轮廓整理：', '法令纹淡化：'):
                assert prompt.count(label) == 1
            assert '除本档位「脸部轮廓」' not in prompt
            assert '不改变下巴形状、颧骨、脸颊丰润度和五官比例' in prompt
            assert ('不瘦脸' in prompt) == (slimming == 'off')
            assert ('保持原肤色明度，不提白' in prompt) == (brightening == 'off')
            assert ('法令纹淡化：保留' in prompt) == (nasolabial == 'off')
            assert '提白只提高明度' not in prompt
        assert '新娘化妆色饱满' in compose_bride_makeup_prompt('refined', 'city_story', detail)
        assert '新郎不化妆' in compose_groom_retouch_prompt('light', detail)


@pytest.mark.parametrize('field', ['slimming', 'brightening', 'concealing', 'chin', 'nasolabial'])
def test_all_eleven_levels_persist_for_each_person_and_produce_distinct_instructions(app_factory, field):
    levels = get_args(ConcealingLevel if field == 'concealing' else AdjustmentLevel)
    assert len(levels) == 11
    with app_factory() as (client, app):
        project = create_project(client)
        url = f"/api/projects/{project['id']}"
        for role in ('bride', 'groom'):
            prompts = []
            for value in levels:
                detail = settings(**{field: value})
                response = client.patch(url, json={f'{role}_retouch_settings': detail})
                assert response.status_code == 200
                assert response.json()[f'{role}_retouch_settings'] == detail
                assert project_state(client, project['id'])[f'{role}_retouch_settings'] == detail
                with app.state.db.connect() as conn:
                    stored = conn.execute(f'SELECT {role}_retouch_settings FROM projects WHERE id=?', (project['id'],)).fetchone()[0]
                assert json.loads(stored) == detail
                validated = RetouchSettings(**detail)
                prompt = (compose_bride_makeup_prompt('natural', 'city_story', validated) if role == 'bride'
                          else compose_groom_retouch_prompt('natural', validated))
                prompts.append(prompt)
                assert '不改变下巴形状、颧骨、脸颊丰润度和五官比例' in prompt
                if field == 'slimming':
                    assert has_face_shaping({'retouch_settings': stored, 'makeup_level': 'natural'}) == (value != 'off')
                    assert ('不瘦脸' in prompt) == (value == 'off')
                if field == 'concealing' and value == 'off':
                    assert '遮瑕程度：关闭，保留原有痘痘、痘印、临时色斑和黑眼圈，不额外遮盖或淡化' in prompt
            assert len(set(prompts)) == 11


@pytest.mark.parametrize('slimming', ['moderate', 'maximum'])
def test_candidates_retry_hair_and_side_looks_keep_the_recorded_settings(app_factory, slimming):
    stylist = ScriptedGenerator([GeneratorError('rejected')])
    with app_factory(stylist=stylist) as (client, app):
        project = create_project(client)
        project_id = project['id']
        original = settings(slimming=slimming, brightening='off', concealing='light', chin='off', nasolabial='off')
        client.patch(f'/api/projects/{project_id}', json={'bride_retouch_settings': original})
        response = client.post(f'/api/projects/{project_id}/makeups', json={
            'client_request_id': 'snapshot', 'person': 'a', 'style_id': 'city_story', 'count': 1})
        assert response.status_code == 201
        makeup_id = response.json()[0]['id']
        failed = wait_until(lambda: makeup_state(client, project_id)[makeup_id], lambda row: row['status'] == 'failed')
        assert failed['retouch_settings'] == original
        original_prompt = stylist.calls[0][1]
        client.patch(f'/api/projects/{project_id}', json={'makeup_level': 'refined'})
        # Replaying a client request returns its original candidate even after the defaults change.
        replay = client.post(f'/api/projects/{project_id}/makeups', json={
            'client_request_id': 'snapshot', 'person': 'a', 'style_id': 'city_story', 'count': 1})
        assert replay.status_code == 200 and replay.json()[0]['retouch_settings'] == original
        assert client.post(f'/api/makeups/{makeup_id}/retry').status_code == 200
        wait_until(lambda: makeup_state(client, project_id)[makeup_id], lambda row: row['status'] == 'ready')
        assert stylist.calls[1][1] == original_prompt
        assert len(stylist.calls) == 2
        look_id = make_bride_looks(client, project_id, ['low_bun'])[0]
        look = next(row for row in project_state(client, project_id)['looks'] if row['id'] == look_id)
        assert look['makeup_level'] == 'natural' and look['retouch_settings'] == original
        assert '不再上妆、不再提白' in stylist.calls[-1][1]
        # Both half-side and profile edits must use the selected look's settings, not current defaults.
        for angle in ('left', 'right_full'):
            assert client.post(f'/api/projects/{project_id}/images', data={'person': 'a', 'angle': angle},
                               files={'file': (angle + '.png', PNG, 'image/png')}).status_code == 200
        assert client.post(f'/api/looks/{look_id}/side-looks').status_code == 201
        wait_until(lambda: project_state(client, project_id)['side_looks'],
                   lambda rows: len(rows) == 2 and all(row['status'] == 'ready' for row in rows))
        with app.state.db.connect() as conn:
            sides = conn.execute('SELECT * FROM looks WHERE parent_look_id=?', (look_id,)).fetchall()
            assert len(sides) == 2
            assert all(json.loads(row['retouch_settings']) == original and row['makeup_level'] == 'natural' for row in sides)
            slimming_text = '适中' if slimming == 'moderate' else '在保持本人辨识度'
            assert all(f'瘦脸程度：{slimming_text}' in row['prompt'] and '肤色提亮：保持原肤色' in row['prompt'] for row in sides)
        # Reuse copies the settings and the hair photos without another generation.
        call_count = len(stylist.calls)
        reused = client.post(f'/api/makeups/{makeup_id}/reuse', json={'style_id': 'flower_house'})
        assert reused.status_code == 200 and reused.json()['retouch_settings'] == original
        copied = next(row for row in project_state(client, project_id)['looks'] if row['style_id'] == 'flower_house')
        assert copied['retouch_settings'] == original and copied['asset_id'] == look['asset_id']
        assert len(stylist.calls) == call_count


@pytest.mark.parametrize('first_role', ['bride', 'groom'])
def test_both_people_use_their_own_settings_even_when_roles_are_swapped(app_factory, first_role):
    with app_factory() as (client, app):
        project = create_project(client, role=first_role)
        project_id = project['id']
        by_role = {'bride': settings(slimming='moderate', brightening='off'),
                   'groom': settings(slimming='light', concealing='full', nasolabial='off')}
        client.patch(f'/api/projects/{project_id}', json={f'{role}_retouch_settings': detail for role, detail in by_role.items()})
        for person in ('a', 'b'):
            role = project[f'person_{person}']['role']
            response = client.post(f'/api/projects/{project_id}/makeups', json={
                'client_request_id': 'role-' + person, 'person': person,
                'style_id': 'city_story' if role == 'bride' else None, 'count': 1})
            assert response.status_code == 201
            row = response.json()[0]
            assert row['role'] == role and row['retouch_settings'] == by_role[role]
            wait_until(lambda: makeup_state(client, project_id)[row['id']], lambda value: value['status'] == 'ready')
            with app.state.db.connect() as conn:
                prompt = conn.execute('SELECT prompt FROM makeups WHERE id=?', (row['id'],)).fetchone()[0]
            detail = RetouchSettings(**by_role[role])
            expected = (compose_bride_makeup_prompt('natural', 'city_story', detail) if role == 'bride'
                        else compose_groom_retouch_prompt('natural', detail))
            assert prompt == expected


def test_side_looks_created_during_shooting_use_the_front_looks_snapshot(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        project_id = project['id']
        original = settings(slimming='moderate', brightening='off', nasolabial='off')
        client.patch(f'/api/projects/{project_id}', json={'bride_retouch_settings': original})
        for person, angle in (('a', 'right'), ('b', 'left')):
            response = client.post(f'/api/projects/{project_id}/images', data={'person': person, 'angle': angle},
                                   files={'file': ('side.png', PNG, 'image/png')})
            assert response.status_code == 200
        wait_batch(client, preview_batch(client, project_id, style_ids=['city_story'])['id'])
        sample = project_state(client, project_id)['previews'][0]
        client.patch(f'/api/projects/{project_id}', json={'makeup_level': 'light'})
        batch = create_director_photo_batch(client, project_id, sample['id'], request_id='side-snapshot')
        assert batch.status_code == 201
        wait_batch(client, batch.json()['id'])
        with app.state.db.connect() as conn:
            sides = conn.execute("SELECT * FROM looks WHERE project_id=? AND role='bride' AND angle<>'front'", (project_id,)).fetchall()
        assert len(sides) == 1
        assert json.loads(sides[0]['retouch_settings']) == original
        assert sides[0]['makeup_level'] == 'natural'
        assert '瘦脸程度：适中' in sides[0]['prompt'] and '肤色提亮：保持原肤色' in sides[0]['prompt']


@pytest.mark.parametrize(('level', 'slimming', 'pick_highest'), [
    ('light', 'moderate', False), ('light', 'minimal', False), ('light', 'maximum', False), ('refined', 'off', True),
])
def test_auto_selection_uses_slimming_instead_of_the_makeup_preset(app_factory, level, slimming, pick_highest):
    with app_factory() as (client, app):
        project = create_project(client)
        project_id = project['id']
        client.patch(f'/api/projects/{project_id}', json={'makeup_level': level,
                     'bride_retouch_settings': settings(slimming=slimming)})
        ids = make_makeups(client, project_id, 2)
        chosen = next(key for key, row in makeup_state(client, project_id).items() if row['selected'])
        other = next(key for key in ids if key != chosen)
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE makeups SET face_score=0.3 WHERE id=?', (chosen,))
            conn.execute('UPDATE makeups SET face_score=0.9 WHERE id=?', (other,))
            auto_select_makeup(conn, conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone(),
                               conn.execute('SELECT * FROM makeups WHERE id=?', (other,)).fetchone())
        assert makeup_state(client, project_id)[other]['selected'] is pick_highest
        # A user choice remains authoritative regardless of scoring and contour settings.
        assert client.post(f'/api/makeups/{chosen}/select').status_code == 200
        with app.state.db.connect(write=True) as conn:
            auto_select_makeup(conn, conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone(),
                               conn.execute('SELECT * FROM makeups WHERE id=?', (other,)).fetchone())
        assert makeup_state(client, project_id)[chosen]['selected_by'] == 'user'


def test_mixed_slimming_requests_are_not_ranked_against_each_other(app_factory):
    with app_factory() as (client, app):
        project = create_project(client)
        project_id = project['id']
        client.patch(f'/api/projects/{project_id}', json={'bride_retouch_settings': settings(slimming='light')})
        first = make_makeups(client, project_id, 1, request_id='slim')[0]
        client.patch(f'/api/projects/{project_id}', json={'bride_retouch_settings': settings(slimming='off')})
        other = make_makeups(client, project_id, 1, request_id='original-contour')[0]
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE makeups SET face_score=0.4 WHERE id=?', (first,))
            conn.execute('UPDATE makeups SET face_score=0.9 WHERE id=?', (other,))
            auto_select_makeup(conn, conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone(),
                               conn.execute('SELECT * FROM makeups WHERE id=?', (other,)).fetchone())
        assert makeup_state(client, project_id)[first]['selected']


def test_legacy_database_upgrade_preserves_candidates_and_their_prompts(app_factory, tmp_path):
    data_dir = tmp_path / 'legacy'
    with app_factory(data_dir=data_dir) as (client, app):
        project = create_project(client)
        project_id = project['id']
        client.patch(f'/api/projects/{project_id}', json={'makeup_level': 'refined'})
        makeup_id = make_makeups(client, project_id, 1)[0]
        legacy_prompt = compose_bride_makeup_prompt('refined', 'city_story')
        with app.state.db.connect(write=True) as conn:
            conn.execute('UPDATE makeups SET prompt=? WHERE id=?', (legacy_prompt, makeup_id))
        db_path = app.state.db.path
    # Reproduce the schema before independent retouch settings existed.
    with sqlite3.connect(db_path) as conn:
        for table, column in (('projects', 'bride_retouch_settings'), ('projects', 'groom_retouch_settings'),
                              ('makeups', 'retouch_settings'), ('looks', 'retouch_settings')):
            conn.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
    db = Database(db_path)
    db.initialize()
    db.initialize()  # Repeated startup must be harmless.
    with app_factory(data_dir=data_dir) as (client, app):
        result = project_state(client, project_id)
        assert result['bride_retouch_settings'] == retouch_preset('refined').model_dump()
        assert result['groom_retouch_settings'] == settings()
        candidate = next(row for row in result['makeups'] if row['id'] == makeup_id)
        assert candidate['usable'] and candidate['retouch_settings'] == retouch_preset('refined').model_dump()
        with app.state.db.connect() as conn:
            row = conn.execute('SELECT * FROM makeups WHERE id=?', (makeup_id,)).fetchone()
            assert row['prompt'] == legacy_prompt and row['retouch_settings'] is None
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_invalid_stored_settings_are_not_replaced_with_a_preset():
    with pytest.raises(ValueError):
        stored_retouch_settings('{"slimming":"unknown"}', 'natural')
