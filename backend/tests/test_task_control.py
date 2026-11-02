"""Task center controls (V7.12): pause a batch or a queued task, continue it, and stop tasks, including one whose
request is already out (its result is discarded, since the provider cannot be told to stop)."""

from conftest import BlockingGenerator, create_project, preview_batch, wait_batch, wait_until

STYLES = ['korean_minimal', 'french_vintage']


def tasks(client, batch_id):
    return sorted(client.get('/api/tasks', params={'batch_id': batch_id}).json(), key=lambda task: task['created_at'])


def by_status(client, batch_id):
    result = {}
    for task in tasks(client, batch_id):
        result.setdefault(task['status'], []).append(task)
    return result


def test_a_paused_batch_holds_its_queue_and_the_follow_ups_until_it_continues(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1, auto_face_fix='ai') as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=STYLES)
        assert generator.started.wait(5)
        paused = client.post(f"/api/batches/{batch['id']}/pause").json()
        assert paused['paused'] and paused['counts']['running'] == 1 and paused['counts']['held'] == 1
        generator.release.set()
        # The running sample finishes; its AI face fix is queued held, like the other sample.
        state = wait_until(lambda: client.get(f"/api/batches/{batch['id']}").json(),
                           lambda batch: batch['counts']['running'] == 0 and batch['counts']['completed'] == 1)
        assert state['status'] == 'held' and state['counts']['held'] == 2 and len(generator.calls) == 1
        resumed = client.post(f"/api/batches/{batch['id']}/continue").json()
        assert not resumed['paused']
        wait_batch(client, batch['id'])
        assert len(generator.calls) == 4  # two samples and their two face fixes


def test_a_single_queued_task_pauses_and_continues(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=STYLES)
        assert generator.started.wait(5)
        running, waiting = by_status(client, batch['id'])['running'][0], by_status(client, batch['id'])['waiting'][0]
        assert client.post(f"/api/tasks/{running['id']}/pause").status_code == 409  # its request is out
        assert client.post(f"/api/tasks/{waiting['id']}/pause").json()['status'] == 'held'
        assert client.post(f"/api/tasks/{waiting['id']}/pause").status_code == 409
        generator.release.set()
        wait_until(lambda: by_status(client, batch['id']), lambda state: 'running' not in state)
        assert len(generator.calls) == 1
        assert client.post(f"/api/tasks/{waiting['id']}/continue").json()['status'] == 'waiting'
        wait_batch(client, batch['id'])
        assert len(generator.calls) == 2


def test_a_running_task_stops_and_its_result_is_discarded(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1, auto_face_fix='ai') as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=STYLES)
        assert generator.started.wait(5)
        running = by_status(client, batch['id'])['running'][0]
        stopped = client.post(f"/api/tasks/{running['id']}/cancel").json()
        assert (stopped['status'], stopped['error_code']) == ('cancelled', 'stopped')
        assert '可能已经扣费' in stopped['error_message']
        generator.release.set()
        # The other sample still runs (the stop does not hold the batch) and the stopped one keeps no picture.
        wait_until(lambda: client.get(f"/api/batches/{batch['id']}").json(),
                   lambda batch: batch['counts']['waiting'] + batch['counts']['running'] == 0)
        slot = next(slot for slot in client.get(f"/api/projects/{project['id']}").json()['previews']
                    if slot['id'] == running['target_slot_id'])
        assert slot['current_version'] is None
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM photo_versions WHERE generation_task_id=?', (running['id'],)).fetchone()[0] == 0
        files = list((app.state.config.data_dir / 'projects' / project['id'] / 'images').glob('*'))
        with app.state.db.connect() as conn:
            known = {row[0] for row in conn.execute('SELECT file_path FROM image_assets')}
        assert all(any(path.as_posix().endswith(item) for item in known) for path in files)


def test_stopping_a_batch_cancels_the_queue_and_the_running_task(app_factory):
    generator = BlockingGenerator()
    with app_factory(generator=generator, concurrency=1) as (client, _app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=STYLES)
        assert generator.started.wait(5)
        state = client.post(f"/api/batches/{batch['id']}/stop").json()
        assert state['counts']['cancelled'] == 2 and state['counts']['running'] == 0 and not state['paused']
        generator.release.set()
        wait_until(lambda: client.get(f"/api/projects/{project['id']}").json(), lambda detail: detail['active_task_count'] == 0)
        assert len(generator.calls) == 1
        assert {task['error_code'] for task in tasks(client, batch['id'])} == {'stopped', None}
