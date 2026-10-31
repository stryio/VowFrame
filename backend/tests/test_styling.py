"""Styled photos (V4.0): one edit per person, style and styling, reused by every shot and face swap of the style."""

import json

from app.db import Database
from app.generator import GeneratorError
from app.storage import asset_path
from conftest import (PNG, ScriptedGenerator, create_director_photo_batch, create_project, preview_batch, uploaded_ids,
                      wait_batch)
from test_face_swap import ScriptedFaceSwapper


def preview_slot(client, project_id, style_id='korean_minimal'):
    return next(slot for slot in client.get(f'/api/projects/{project_id}').json()['previews'] if slot['style_id'] == style_id)


def test_a_set_shot_in_parallel_edits_each_styled_photo_once(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=4) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        # A new level after the sample: the set needs a new styled photo of the bride, and its four shots run at once.
        client.patch(f"/api/projects/{project['id']}", json={'makeup_level': 'refined'})
        batch = create_director_photo_batch(client, project['id'], preview_slot(client, project['id'])['id'], request_id='set')
        wait_batch(client, batch.json()['id'])
        prompts = [call[1] for call in app.state.stylist.calls]
        assert len(prompts) == 3 and sum('新娘化精致的影楼新娘妆' in prompt for prompt in prompts) == 1
        with app.state.db.connect() as conn:
            fronts = {tuple(json.loads(row[0])[:2]) for row in conn.execute(
                'SELECT reference_image_ids FROM generation_tasks WHERE batch_id=?', (batch.json()['id'],))}
        assert len(fronts) == 1


def test_styled_photos_are_per_style(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal', 'hanfu'])['id'])
        korean, hanfu = preview_slot(client, project['id']), preview_slot(client, project['id'], 'hanfu')
        assert set(korean['styled_asset_ids'].values()).isdisjoint(hanfu['styled_asset_ids'].values())
        assert sum('戴宋制幞头' in call[1] for call in app.state.stylist.calls) == 1
        with app.state.db.connect() as conn:
            rows = conn.execute('SELECT style_id,role,angle,makeup_level,hair_length,styling_version FROM styled_photos '
                                'ORDER BY style_id,role').fetchall()
        assert [tuple(row) for row in rows] == [
            ('hanfu', 'bride', 'front', 'natural', 'long', 1), ('hanfu', 'groom', 'front', 'natural', 'short', 1),
            ('korean_minimal', 'bride', 'front', 'natural', 'long', 1), ('korean_minimal', 'groom', 'front', 'natural', 'short', 1),
        ]


def test_automatic_face_swap_uses_the_styled_photos(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, auto_face_swap=True) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        styled = preview_slot(client, project['id'])['styled_asset_ids']
        with app.state.db.connect() as conn:
            paths = {row['id']: row['file_path'] for row in conn.execute('SELECT id,file_path FROM image_assets')}
        bride, groom = swapper.calls[0][0], swapper.calls[0][1]
        assert bride.front.as_posix().endswith(paths[styled['bride']]) and groom.front.as_posix().endswith(paths[styled['groom']])


def test_a_failed_styling_edit_fails_the_shot_and_a_retry_keeps_the_finished_one(app_factory):
    stylist = ScriptedGenerator([None, GeneratorError('rejected')])
    with app_factory(generator=ScriptedGenerator(), stylist=stylist) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        task = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
        assert task['error_code'] == 'rejected' and app.state.generator.calls == []
        retry = client.post(f"/api/tasks/{task['id']}/retry")
        assert retry.status_code in (200, 201), retry.text
        wait_batch(client, batch['id'])
        # The bride's styled photo from the first try is reused; only the groom's is edited again.
        assert ['新娘' in call[1].split('\n\n')[0] for call in stylist.calls] == [True, False, False]


def test_styled_photos_go_with_their_project(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        styled = preview_slot(client, project['id'])['styled_asset_ids']
        assert uploaded_ids(app, list(styled.values())) == [project['person_a']['asset_id'], project['person_b']['asset_id']]
        assert client.delete(f"/api/projects/{project['id']}").status_code == 204
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM styled_photos').fetchone()[0] == 0
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def styled_rows(app, *, discarded=False):
    with app.state.db.connect() as conn:
        return [dict(row) for row in conn.execute(
            'SELECT * FROM styled_photos WHERE discarded_at IS ' + ('NOT NULL' if discarded else 'NULL') + ' ORDER BY rowid')]


def task_row(app, task_id):
    with app.state.db.connect() as conn:
        return dict(conn.execute('SELECT * FROM generation_tasks WHERE id=?', (task_id,)).fetchone())


def test_a_styled_photo_whose_file_is_gone_is_edited_again(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        styled = preview_slot(client, project['id'])['styled_asset_ids']
        with app.state.db.connect() as conn:
            stored = conn.execute('SELECT file_path FROM image_assets WHERE id=?', (styled['bride'],)).fetchone()[0]
        asset_path(app.state.config, stored).unlink()
        # Without the file the record is useless: the next shot retires it and edits the bride's photo again.
        again = preview_batch(client, project['id'], request_id='again', style_ids=['korean_minimal'])
        wait_batch(client, again['id'])
        assert len(app.state.stylist.calls) == 3 and '新娘' in app.state.stylist.calls[2][1].split('\n\n')[0]
        assert [row['image_asset_id'] for row in styled_rows(app, discarded=True)] == [styled['bride']]
        current = preview_slot(client, project['id'])['styled_asset_ids']
        assert current['bride'] != styled['bride'] and current['groom'] == styled['groom']
        assert uploaded_ids(app, [current['bride']]) == [project['person_a']['asset_id']]


def test_a_restart_during_styling_interrupts_the_shot_and_a_retry_keeps_the_finished_photo(app_factory, tmp_path):
    data_dir = tmp_path / 'restart'
    stylist = ScriptedGenerator([None, GeneratorError('rejected')])
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, data_dir=data_dir, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'], status='needs_attention')
        task = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
        # Put the task back where a restart would find it: the bride's photo is styled, the groom's edit is running.
        with app.state.db.connect(write=True) as conn:
            conn.execute("UPDATE generation_tasks SET status='running',error_code=NULL,error_message=NULL,finished_at=NULL "
                         'WHERE id=?', (task['id'],))
        assert len(styled_rows(app)) == 1
    stylist = ScriptedGenerator()
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, data_dir=data_dir, concurrency=1) as (client, app):
        interrupted = task_row(app, task['id'])
        assert interrupted['status'] == 'interrupted' and interrupted['error_code'] == 'interrupted_by_restart'
        # The styled photos are recorded on the task only once all of them are ready.
        assert json.loads(interrupted['reference_image_ids']) == [project['person_a']['asset_id'], project['person_b']['asset_id']]
        assert stylist.calls == [] and app.state.generator.calls == []
        retry = client.post(f"/api/tasks/{task['id']}/retry")
        assert retry.status_code in (200, 201), retry.text
        wait_batch(client, batch['id'])
        assert len(stylist.calls) == 1 and '新郎' in stylist.calls[0][1].split('\n\n')[0]
        assert len(styled_rows(app)) == 2


def test_a_failed_side_photo_edit_holds_the_set_and_a_retry_reuses_the_fronts(app_factory):
    stylist = ScriptedGenerator([None, None, GeneratorError('auth_or_quota')])
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, concurrency=1) as (client, app):
        project = create_project(client)
        for person, angle in (('a', 'right'), ('b', 'left')):
            response = client.post(f"/api/projects/{project['id']}/images", data={'person': person, 'angle': angle},
                                   files={'file': (f'{person}-{angle}.png', PNG, 'image/png')})
            assert response.status_code == 200, response.text
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        fronts = styled_rows(app)
        batch = create_director_photo_batch(client, project['id'], preview_slot(client, project['id'])['id'], request_id='set')
        assert batch.status_code == 201, batch.text
        batch_id = batch.json()['id']
        # Mock shots: the second needs the groom facing left, whose styling edit is refused for lack of balance.
        tasks = wait_batch(client, batch_id, status='held') and client.get('/api/tasks', params={'batch_id': batch_id}).json()
        tasks.sort(key=lambda item: item['created_at'])
        assert [item['status'] for item in tasks] == ['completed', 'failed', 'held', 'held']
        assert tasks[1]['error_code'] == 'auth_or_quota'
        assert client.post(f"/api/tasks/{tasks[1]['id']}/retry").status_code in (200, 201)
        assert client.post(f'/api/batches/{batch_id}/continue').status_code == 200
        wait_batch(client, batch_id)
        # The fronts are never edited again; each side photo is edited once more, from the side photo and the styled front.
        side_edits = [call for call in stylist.calls[2:] if '脸朝画面' in call[1]]
        assert len(stylist.calls) == 5 and len(side_edits) == 3 and all(len(call[0]) == 2 for call in side_edits)
        rows = styled_rows(app)
        assert rows[:2] == fronts and sorted((row['role'], row['angle']) for row in rows[2:]) == [('bride', 'right'), ('groom', 'left')]


def test_an_old_picture_replays_its_own_styled_photos_after_restyling(app_factory):
    with app_factory(generator=ScriptedGenerator(), concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        slot = preview_slot(client, project['id'])
        old_version, old_styled = slot['current_version']['id'], slot['styled_asset_ids']
        restyled = client.post(f"/api/projects/{project['id']}/previews",
                               json={'client_request_id': 'restyle', 'style_ids': ['korean_minimal'], 'restyle': True})
        wait_batch(client, restyled.json()['id'])
        new_styled = preview_slot(client, project['id'])['styled_asset_ids']
        assert set(new_styled.values()).isdisjoint(old_styled.values())
        restored = client.post(f"/api/slots/{slot['id']}/restore", json={'version_id': old_version})
        assert restored.status_code == 200 and restored.json()['styled_asset_ids'] == old_styled
        # 'same' replays the old picture as it was, with its discarded styled photos and no new edit.
        same = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'same', 'mode': 'same'})
        wait_batch(client, same.json()['id'])
        assert len(app.state.stylist.calls) == 4
        assert preview_slot(client, project['id'])['styled_asset_ids'] == old_styled
        # A new shot uses the current styled photos.
        batch = create_director_photo_batch(client, project['id'], slot['id'], request_id='set')
        wait_batch(client, batch.json()['id'])
        tasks = client.get('/api/tasks', params={'batch_id': batch.json()['id']}).json()
        assert {tuple(json.loads(task_row(app, task['id'])['reference_image_ids'])[:2]) for task in tasks} == {
            (new_styled['bride'], new_styled['groom'])}
        assert len(app.state.stylist.calls) == 4


def test_pictures_from_before_styled_photos_replay_and_swap_with_the_uploaded_photos(app_factory):
    swapper = ScriptedFaceSwapper()
    with app_factory(generator=ScriptedGenerator(), face_swapper=swapper, concurrency=1) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=['korean_minimal'])
        wait_batch(client, batch['id'])
        task = client.get('/api/tasks', params={'batch_id': batch['id']}).json()[0]
        uploads = [project['person_a']['asset_id'], project['person_b']['asset_id']]
        # Turn the sample into a pre-V4.0 picture: no styling in its snapshot, the uploaded photos as references.
        with app.state.db.connect(write=True) as conn:
            snapshot = json.loads(task_row(app, task['id'])['model_config_snapshot'])
            snapshot.pop('styling'), snapshot.pop('reference_slots')
            conn.execute('UPDATE generation_tasks SET model_config_snapshot=?,reference_image_ids=?,prompt=? WHERE id=?',
                         (json.dumps(snapshot), json.dumps(uploads), 'legacy prompt', task['id']))
            paths = {row['id']: row['file_path'] for row in conn.execute('SELECT id,file_path FROM image_assets')}
        slot = preview_slot(client, project['id'])
        assert slot['styled_asset_ids'] is None
        same = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'same', 'mode': 'same'})
        wait_batch(client, same.json()['id'])
        replay = task_row(app, client.get('/api/tasks', params={'batch_id': same.json()['id']}).json()[0]['id'])
        assert json.loads(replay['reference_image_ids']) == uploads and replay['prompt'] == 'legacy prompt'
        assert '造型照' not in json.dumps(app.state.generator.calls[-1][1]) and len(app.state.stylist.calls) == 2
        swap = client.post(f"/api/slots/{slot['id']}/face-swap", json={'client_request_id': 'swap'})
        assert swap.status_code == 201, swap.text
        wait_batch(client, swap.json()['id'])
        bride, groom = swapper.calls[-1][0], swapper.calls[-1][1]
        assert bride.front.as_posix().endswith(paths[uploads[0]]) and groom.front.as_posix().endswith(paths[uploads[1]])
        assert len(app.state.stylist.calls) == 2


def test_an_old_database_gets_the_styled_photo_table(tmp_path):
    db = Database(tmp_path / 'old.db')
    db.initialize()
    with db.connect(write=True) as conn:
        conn.execute('DROP TABLE styled_photos')
    db.initialize()
    with db.connect() as conn:
        columns = [row['name'] for row in conn.execute('PRAGMA table_info(styled_photos)')]
        foreign_keys = {(row['from'], row['table'], row['on_delete']) for row in conn.execute('PRAGMA foreign_key_list(styled_photos)')}
        indexes = {row['name'] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='styled_photos'")}
    assert columns == ['id', 'project_id', 'style_id', 'role', 'angle', 'source_asset_id', 'makeup_level', 'hair_length',
                       'styling_version', 'image_asset_id', 'prompt', 'created_at', 'discarded_at']
    assert foreign_keys == {('project_id', 'projects', 'CASCADE'), ('style_id', 'style_presets', 'NO ACTION'),
                            ('source_asset_id', 'image_assets', 'NO ACTION'), ('image_asset_id', 'image_assets', 'NO ACTION')}
    assert 'styled_photo_key' in indexes
