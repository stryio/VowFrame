"""Per-shot portrait/landscape frames: director choice → task snapshot → image model."""

import json

from app.aspect import orientation_of, ratio_for
from conftest import ScriptedGenerator, create_project, preview_batch, wait_batch


def ready_preview(client, project_id):
    batch = preview_batch(client, project_id, style_ids=['korean_minimal'])
    wait_batch(client, batch['id'])
    return client.get(f'/api/projects/{project_id}').json()['previews'][0]


def make_draft(client, project_id, preview_id, count=4):
    response = client.post(f'/api/projects/{project_id}/moment-drafts', json={'preview_slot_id': preview_id, 'count': count})
    assert response.status_code == 201, response.text
    return response.json()


def shoot(client, project_id, draft_id, request_id='frames'):
    response = client.post(f'/api/projects/{project_id}/photo-batches', json={'client_request_id': request_id, 'draft_id': draft_id})
    assert response.status_code == 201, response.text
    wait_batch(client, response.json()['id'])
    return client.get('/api/tasks', params={'batch_id': response.json()['id']}).json()


def test_ratio_pairs_turn_the_portrait_ratio_sideways():
    assert [ratio_for('landscape', ratio) for ratio in ('3:4', '2:3', '9:16')] == ['4:3', '3:2', '16:9']
    assert ratio_for('portrait', '9:16') == '9:16'
    assert orientation_of('16:9') == 'landscape' and orientation_of('3:4') == 'portrait'


def test_each_shot_is_generated_in_the_frame_the_director_chose(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, _app):
        project = create_project(client)
        preview = ready_preview(client, project['id'])
        assert generator.aspect_ratios == ['3:4']  # samples are always portrait
        draft = make_draft(client, project['id'], preview['id'])
        orientations = [moment['orientation'] for moment in draft['moments']]
        assert orientations == ['landscape', 'portrait', 'portrait', 'portrait']  # mock director order
        tasks = shoot(client, project['id'], draft['id'])
        by_title = {moment['title']: moment['orientation'] for moment in draft['moments']}
        for task in tasks:
            expected = '4:3' if by_title[task['moment_title']] == 'landscape' else '3:4'
            assert task['prompt'].startswith(('横构图，4:3' if expected == '4:3' else '竖构图，3:4'))
        assert sorted(generator.aspect_ratios[1:]) == ['3:4', '3:4', '3:4', '4:3']


def test_portrait_ratio_setting_decides_both_frames(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, _app):
        assert client.patch('/api/settings', json={'portrait_ratio': '9:16'}).status_code == 200
        project = create_project(client)
        preview = ready_preview(client, project['id'])
        draft = make_draft(client, project['id'], preview['id'])
        shoot(client, project['id'], draft['id'])
        assert generator.aspect_ratios[0] == '9:16'
        assert sorted(generator.aspect_ratios[1:]) == ['16:9', '9:16', '9:16', '9:16']


def test_plan_preview_can_flip_a_shot_between_portrait_and_landscape(app_factory):
    with app_factory(concurrency=1) as (client, _app):
        project = create_project(client)
        preview = ready_preview(client, project['id'])
        draft = make_draft(client, project['id'], preview['id'])
        flipped = client.patch(f"/api/moment-drafts/{draft['id']}/moments/1", json={'orientation': 'landscape'})
        assert flipped.status_code == 200, flipped.text
        assert flipped.json()['moments'][1]['orientation'] == 'landscape'
        invalid = client.patch(f"/api/moment-drafts/{draft['id']}/moments/1", json={'orientation': 'square'})
        assert invalid.status_code == 422


def test_slot_actions_keep_the_source_frame_even_after_the_setting_changes(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, app):
        project = create_project(client)
        preview = ready_preview(client, project['id'])
        draft = make_draft(client, project['id'], preview['id'])
        tasks = shoot(client, project['id'], draft['id'])
        landscape = next(task for task in tasks if task['prompt'].startswith('横构图'))
        client.patch('/api/settings', json={'portrait_ratio': '9:16'})

        boost = client.post(f"/api/slots/{landscape['target_slot_id']}/regenerate",
                            json={'client_request_id': 'boost', 'mode': 'identity_boost'})
        assert boost.status_code == 201, boost.text
        wait_batch(client, boost.json()['id'])
        shot = client.post(f"/api/slots/{landscape['target_slot_id']}/change-shot",
                           json={'client_request_id': 'shot', 'shot_size': 'half'})
        assert shot.status_code == 201, shot.text
        wait_batch(client, shot.json()['id'])
        assert generator.aspect_ratios[-2:] == ['4:3', '4:3']
        with app.state.db.connect() as conn:
            snapshots = [json.loads(row[0]) for row in conn.execute(
                'SELECT model_config_snapshot FROM generation_tasks WHERE target_slot_id=? ORDER BY created_at',
                (landscape['target_slot_id'],))]
        assert [item['aspect_ratio'] for item in snapshots] == ['4:3', '4:3', '4:3']


def test_drafts_saved_before_frames_existed_load_as_portrait(app_factory):
    with app_factory(concurrency=1) as (client, app):
        project = create_project(client)
        preview = ready_preview(client, project['id'])
        draft = make_draft(client, project['id'], preview['id'])
        with app.state.db.connect(write=True) as conn:
            moments = json.loads(conn.execute('SELECT moments FROM moment_drafts WHERE id=?', (draft['id'],)).fetchone()[0])
            legacy = [{key: value for key, value in moment.items() if key != 'orientation'} for moment in moments]
            conn.execute('UPDATE moment_drafts SET moments=? WHERE id=?', (json.dumps(legacy, ensure_ascii=False), draft['id']))
        renamed = client.patch(f"/api/moment-drafts/{draft['id']}/moments/0", json={'title': '旧方案改名'})
        assert renamed.status_code == 200, renamed.text
        assert all(moment['orientation'] == 'portrait' for moment in renamed.json()['moments'])
