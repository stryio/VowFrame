"""Final looks (V7.18): a selected look edited into the whole person in an outfit. It tells a sample what the clothes
are, while the head-and-shoulders looks stay first and keep fixing the face."""

import json
from pathlib import Path

from conftest import ScriptedGenerator, create_director_photo_batch, create_project, preview_batch, wait_batch, wait_until
from app.generator import GeneratorError, OutcomeKind
from app.prompts import OUTFIT_BY_ID, compose_outfit_look_prompt
from test_styling import batch_tasks, detail, make_looks, task_row

DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
FINAL_RULE = '全身定妆照只用来确定服饰'


def prepared(client, style_id='korean_minimal'):
    project = create_project(client)
    bride = make_looks(client, project['id'], 'a', [{'hairstyle_id': 'low_bun'}], style_id=style_id)[0]
    groom = make_looks(client, project['id'], 'b', [{'hairstyle_id': 'keep'}])[0]
    return project, bride, groom


def make_final(client, project_id, look_id, outfit_id='studio_satin', request_id=None):
    response = client.post(f'/api/projects/{project_id}/outfit-looks', json={
        'client_request_id': request_id or f'{look_id}-{outfit_id}', 'look_id': look_id, 'outfit_id': outfit_id})
    assert response.status_code == 201, response.text
    row_id = response.json()['id']
    return wait_until(lambda: next(row for row in detail(client, project_id)['outfit_looks'] if row['id'] == row_id),
                      lambda row: row['status'] not in ('waiting', 'running'))


def looks_of(client, project_id, ids):
    return next(look['asset_id'] for look in detail(client, project_id)['looks'] if look['id'] == ids)


def stored(app, batch_tasks_row):
    row = task_row(app, batch_tasks_row['id'])
    return json.loads(row['reference_image_ids']), json.loads(row['model_config_snapshot'])


def test_the_prompts_match_the_doc():
    for role in ('bride', 'groom'):
        paragraphs = compose_outfit_look_prompt(role, 'satin_a_line').split('\n\n')
        assert paragraphs[0] in DOC
        assert all(paragraph in DOC for paragraph in paragraphs[2:])
    assert OUTFIT_BY_ID['satin_a_line'].headwear in compose_outfit_look_prompt('bride', 'satin_a_line')
    assert OUTFIT_BY_ID['satin_a_line'].headwear not in compose_outfit_look_prompt('groom', 'satin_a_line')
    assert FINAL_RULE in DOC


def test_a_final_look_is_one_edit_of_the_look_and_the_first_is_selected(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project, bride, _groom = prepared(client)
        before = len(app.state.stylist.calls)
        first = make_final(client, project['id'], bride)
        calls = app.state.stylist.calls[before:]
        assert len(calls) == 1 and len(calls[0][0]) == 1
        assert calls[0][0][0].stem == looks_of(client, project['id'], bride)
        assert (first['status'], first['selected'], first['selected_by'], first['usable']) == ('ready', True, 'auto', True)

        second = make_final(client, project['id'], bride, request_id='second')
        assert second['selected'] is False
        assert client.post(f"/api/outfit-looks/{second['id']}/select").json()['selected_by'] == 'user'
        rows = {row['id']: row for row in detail(client, project['id'])['outfit_looks']}
        assert rows[first['id']]['selected'] is False
        assert client.delete(f"/api/outfit-looks/{second['id']}").status_code == 204
        assert detail(client, project['id'])['outfit_looks'][0]['selected_by'] == 'auto'


def test_bad_requests_are_refused_and_a_repeated_request_is_not_paid_twice(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project, bride, groom = prepared(client)
        url = f"/api/projects/{project['id']}/outfit-looks"
        wrong = client.post(url, json={'client_request_id': 'wrong', 'look_id': bride, 'outfit_id': 'xiuhe'})
        assert wrong.status_code == 422 and wrong.json()['detail']['code'] == 'invalid_outfit'
        missing = client.post(url, json={'client_request_id': 'missing', 'look_id': 'nowhere', 'outfit_id': 'satin_a_line'})
        assert missing.status_code == 422 and missing.json()['detail']['code'] == 'invalid_look'
        # The groom's look serves every style, so any outfit in use.
        make_final(client, project['id'], groom, 'xiuhe', request_id='same')
        count = len(app.state.stylist.calls)
        again = client.post(url, json={'client_request_id': 'same', 'look_id': groom, 'outfit_id': 'xiuhe'})
        assert again.status_code == 200 and len(app.state.stylist.calls) == count
        reused = client.post(url, json={'client_request_id': 'same', 'look_id': bride, 'outfit_id': 'satin_a_line'})
        assert reused.status_code == 409


def test_a_sample_sends_the_final_looks_after_the_looks_and_keeps_faces_in_place(client_app):
    client, app = client_app
    project, bride, groom = prepared(client)
    bride_final = make_final(client, project['id'], bride)
    groom_final = make_final(client, project['id'], groom)
    batch = preview_batch(client, project['id'], request_id='sample', style_ids=['korean_minimal'])
    wait_batch(client, batch['id'])
    task = batch_tasks(client, batch['id'])[0]
    references, snapshot = stored(app, task)
    assert references[:2] == [looks_of(client, project['id'], bride), looks_of(client, project['id'], groom)]
    assert references[2:] == [bride_final['asset_id'], groom_final['asset_id']]
    assert '第三张图是新娘穿这套服饰的全身定妆照，第四张图是新郎穿这套服饰的全身定妆照' in task['prompt'] and FINAL_RULE in task['prompt']
    assert snapshot['reference_slots'] == [['bride', 'front'], ['groom', 'front'], None, None]
    assert snapshot['outfit_references'] == {'bride': bride_final['asset_id'], 'groom': groom_final['asset_id']}


def test_without_a_final_look_a_sample_has_only_the_text_and_one_person_can_have_it_alone(client_app):
    client, app = client_app
    project, bride, _groom = prepared(client)
    plain = preview_batch(client, project['id'], request_id='plain', style_ids=['korean_minimal'])
    wait_batch(client, plain['id'])
    task = batch_tasks(client, plain['id'])[0]
    assert len(stored(app, task)[0]) == 2 and FINAL_RULE not in task['prompt']

    final = make_final(client, project['id'], bride)
    alone = preview_batch(client, project['id'], request_id='alone', style_ids=['korean_minimal'])
    wait_batch(client, alone['id'])
    task = batch_tasks(client, alone['id'])[0]
    assert stored(app, task)[0][2:] == [final['asset_id']]
    assert '第三张图是新娘穿这套服饰的全身定妆照' in task['prompt'] and '第四张图' not in task['prompt']


def test_changing_the_look_stops_using_the_old_final_look(client_app):
    client, app = client_app
    project, bride, _groom = prepared(client)
    make_final(client, project['id'], bride)
    make_looks(client, project['id'], 'a', [{'hairstyle_id': 'half_up'}], style_id='korean_minimal', request_id='other')
    batch = preview_batch(client, project['id'], request_id='after', style_ids=['korean_minimal'])
    wait_batch(client, batch['id'])
    assert len(stored(app, batch_tasks(client, batch['id'])[0])[0]) == 2


def test_a_set_sends_the_final_looks_only_without_the_sample_and_a_face_fix_never_does(client_app):
    client, app = client_app
    project, bride, groom = prepared(client)
    bride_final = make_final(client, project['id'], bride)
    groom_final = make_final(client, project['id'], groom)
    batch = preview_batch(client, project['id'], request_id='sample', style_ids=['korean_minimal'])
    wait_batch(client, batch['id'])
    sample = detail(client, project['id'])['previews'][0]

    client.patch('/api/settings', json={'batch_reference_mode': 'with_preview'})
    photos = create_director_photo_batch(client, project['id'], sample['id'], request_id='with').json()
    wait_batch(client, photos['id'])
    tasks = batch_tasks(client, photos['id'])
    assert tasks and all(FINAL_RULE not in task['prompt'] for task in tasks)
    assert all(bride_final['asset_id'] not in stored(app, task)[0] for task in tasks)

    client.patch('/api/settings', json={'batch_reference_mode': 'prompt_only'})
    photos = create_director_photo_batch(client, project['id'], sample['id'], request_id='without').json()
    wait_batch(client, photos['id'])
    tasks = batch_tasks(client, photos['id'])
    assert tasks and all(FINAL_RULE in task['prompt'] for task in tasks)
    references = stored(app, tasks[0])[0]
    assert references[-2:] == [bride_final['asset_id'], groom_final['asset_id']]

    # The face sources stay the head-and-shoulders looks.
    slot = next(item for item in detail(client, project['id'])['photos'] if item['batch_id'] == photos['id'])
    fixed = client.post(f"/api/slots/{slot['id']}/face-fix", json={'client_request_id': 'fix'})
    assert fixed.status_code == 201, fixed.text
    fix_task = next(task for task in batch_tasks(client, fixed.json()['id']))
    assert stored(app, fix_task)[0] == references[:2]


def test_a_failed_final_look_can_be_retried_and_is_never_used_meanwhile(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator) as (client, app):
        project, bride, _groom = prepared(client)
        original = app.state.stylist.generate_image
        def refuse(*_args):
            raise GeneratorError(OutcomeKind.REJECTED, '不行')
        app.state.stylist.generate_image = refuse
        failed = make_final(client, project['id'], bride)
        assert failed['status'] == 'failed' and failed['selected'] is False
        batch = preview_batch(client, project['id'], request_id='sample', style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        assert len(stored(app, batch_tasks(client, batch['id'])[0])[0]) == 2
        app.state.stylist.generate_image = original
        assert client.post(f"/api/outfit-looks/{failed['id']}/retry").status_code == 200
        done = wait_until(lambda: detail(client, project['id'])['outfit_looks'][0], lambda row: row['status'] == 'ready')
        assert done['selected_by'] == 'auto'


def test_boosting_a_sample_keeps_the_final_looks_it_wore(client_app):
    client, app = client_app
    project, bride, _groom = prepared(client)
    final = make_final(client, project['id'], bride)
    batch = preview_batch(client, project['id'], request_id='sample', style_ids=['korean_minimal'])
    wait_batch(client, batch['id'])
    # Deleting the candidate later does not change what the sample's next shot wears.
    assert client.delete(f"/api/outfit-looks/{final['id']}").status_code == 204
    sample = detail(client, project['id'])['previews'][0]
    boost = client.post(f"/api/slots/{sample['id']}/regenerate", json={'client_request_id': 'boost', 'mode': 'identity_boost'})
    assert boost.status_code == 201, boost.text
    task = batch_tasks(client, boost.json()['id'])[0]
    assert stored(app, task)[0][2:] == [final['asset_id']] and FINAL_RULE in task['prompt']
