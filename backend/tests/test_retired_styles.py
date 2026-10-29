"""Retired styles (V3.20): hidden from new samples, still usable where a project already has them."""

from conftest import create_project, preview_batch, wait_batch
from app.prompts import DEFAULT_PREVIEW_STYLE_IDS, RETIRED_STYLE_IDS, STYLE_BY_ID


def test_styles_endpoint_flags_retired_styles_and_defaults_skip_them(client_app):
    client, _ = client_app
    styles = {s['id']: s for s in client.get('/api/styles').json()}
    assert {sid for sid, s in styles.items() if s['retired']} == RETIRED_STYLE_IDS
    assert not RETIRED_STYLE_IDS & set(DEFAULT_PREVIEW_STYLE_IDS) and len(DEFAULT_PREVIEW_STYLE_IDS) == 6
    assert all(sid in STYLE_BY_ID for sid in RETIRED_STYLE_IDS)


def test_new_samples_reject_retired_styles(client_app):
    client, _ = client_app
    project = create_project(client)
    response = client.post(f"/api/projects/{project['id']}/previews",
                           json={'client_request_id': 'retired', 'style_ids': ['korean_minimal', 'black_white']})
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'invalid_styles'
    assert '黑白经典' in response.json()['detail']['message']


def test_existing_sample_of_a_retired_style_can_be_regenerated(app_factory):
    with app_factory(concurrency=1) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=['korean_minimal'])['id'])
        # A sample made before the style was retired: move it to a retired style.
        with app.state.db.connect(write=True) as conn:
            conn.execute("UPDATE photo_slots SET style_id='black_white' WHERE project_id=? AND kind='preview'", (project['id'],))
            conn.execute("UPDATE generation_tasks SET style_id='black_white' WHERE project_id=?", (project['id'],))
        again = preview_batch(client, project['id'], request_id='again', style_ids=['black_white'])
        wait_batch(client, again['id'])
        previews = client.get(f"/api/projects/{project['id']}").json()['previews']
        assert [p['style_id'] for p in previews] == ['black_white']
