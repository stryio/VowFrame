"""Fixed outfits (V7.17): the text a sample wears is what its set, its director and its later shots wear."""

from pathlib import Path

from conftest import create_director_photo_batch, create_project, preview_batch, wait_batch
from app.prompts import (OUTFIT_BY_ID, OUTFIT_RULE, OUTFITS, RETIRED_OUTFIT_IDS, RETIRED_STYLE_IDS, STYLE_BY_ID, STYLE_OUTFITS, compose_moment_prompt,
                         director_scene_text, outfit_text)
from test_styling import batch_tasks, detail, make_looks

DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()


def test_every_active_style_has_outfits_and_the_doc_has_their_text():
    # Styles retired after they got outfits (V7.23) keep them, so their existing samples refresh in the same outfit.
    assert set(STYLE_OUTFITS) >= set(STYLE_BY_ID) - RETIRED_STYLE_IDS
    offered = {outfit for outfits in STYLE_OUTFITS.values() for outfit in outfits}
    # V7.51: the retired hat outfits are offered by no style but kept for the pictures shot in them.
    assert offered | RETIRED_OUTFIT_IDS == set(OUTFIT_BY_ID) and not offered & RETIRED_OUTFIT_IDS
    assert len(OUTFITS) == len(OUTFIT_BY_ID)
    assert OUTFIT_RULE in DOC
    for outfit in OUTFITS:
        assert outfit.bride in DOC and outfit.groom in DOC and outfit.headwear in DOC


def test_a_shot_and_the_director_read_the_outfit_not_the_old_costume():
    prompt = compose_moment_prompt('chinese_traditional', '50mm 镜头。', '3:4', scene=1, styled=True, outfit='xiuhe')
    assert outfit_text('xiuhe') in prompt and OUTFIT_RULE in prompt
    assert '长袍马褂' not in prompt and '中式金饰头饰' not in prompt
    scene = director_scene_text('chinese_traditional', single_variant=True, outfit='xiuhe')
    assert outfit_text('xiuhe') in scene and '妆容方向' not in scene
    # Pictures from before outfits keep the old text.
    assert '长袍马褂' in compose_moment_prompt('chinese_traditional', '50mm 镜头。', '3:4', scene=1, styled=True)


def test_a_sample_wears_the_chosen_outfit_and_its_set_keeps_it_after_the_choice_changes(client_app, monkeypatch):
    # V7.73: the sample moved to new_chinese is made without a set, as that style's samples were.
    from app import rooms
    monkeypatch.setitem(rooms.STYLE_ROOMS, 'city_story', ())
    client, app = client_app
    project = create_project(client)
    # new_chinese is the one style with two outfits, retired in V7.23: give the project a sample made before that.
    wait_batch(client, preview_batch(client, project['id'], request_id='before', style_ids=['city_story'])['id'])
    with app.state.db.connect(write=True) as conn:
        conn.execute("UPDATE photo_slots SET style_id='new_chinese' WHERE project_id=? AND kind='preview'", (project['id'],))
        conn.execute("UPDATE generation_tasks SET style_id='new_chinese' WHERE project_id=?", (project['id'],))
    assert detail(client, project['id'])['style_outfits']['new_chinese'] == 'new_chinese_gown'
    url = f"/api/projects/{project['id']}/style-outfits/new_chinese"
    assert client.put(url, json={'outfit_id': 'xiuhe'}).status_code == 422
    assert client.put(url, json={'outfit_id': 'mamian'}).json()['style_outfits']['new_chinese'] == 'mamian'
    batch = preview_batch(client, project['id'], request_id='mamian', style_ids=['new_chinese'])
    wait_batch(client, batch['id'])
    assert OUTFIT_BY_ID['mamian'].bride in batch_tasks(client, batch['id'])[0]['prompt']

    client.put(url, json={'outfit_id': 'new_chinese_gown'})
    sample = detail(client, project['id'])['previews'][0]
    photos = create_director_photo_batch(client, project['id'], sample['id'], request_id='set').json()
    wait_batch(client, photos['id'])
    prompts = [task['prompt'] for task in batch_tasks(client, photos['id'])]
    assert prompts and all(OUTFIT_BY_ID['mamian'].bride in prompt for prompt in prompts)

    slot = detail(client, project['id'])['photos'][0]
    boost = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'boost', 'mode': 'identity_boost'})
    assert boost.status_code == 201, boost.text
    assert OUTFIT_BY_ID['mamian'].bride in batch_tasks(client, boost.json()['id'])[0]['prompt']
    # The next sample wears the new choice.
    again = preview_batch(client, project['id'], request_id='gown', style_ids=['new_chinese'])
    assert OUTFIT_BY_ID['new_chinese_gown'].bride in batch_tasks(client, again['id'])[0]['prompt']


def test_a_retired_outfit_cannot_be_picked_and_an_old_choice_falls_back_to_the_first(app_factory):
    """V7.51: the French hat outfits are no longer offered (V7.60: nor the three after them)."""
    from conftest import create_project
    with app_factory() as (client, app):
        project = create_project(client)
        response = client.put(f"/api/projects/{project['id']}/style-outfits/french_couture", json={'outfit_id': 'french_flower_hat'})
        assert response.status_code == 422
        with app.state.db.connect(write=True) as conn:  # a choice made before V7.51
            conn.execute('INSERT INTO project_outfits (project_id,style_id,outfit_id) VALUES (?,?,?)',
                         (project['id'], 'french_couture', 'french_rose_taffeta'))
        assert client.get(f"/api/projects/{project['id']}").json()['style_outfits']['french_couture'] == 'french_lace_offshoulder'
