"""Fixed outfits (V7.17): the text a sample wears is what its set, its director and its later shots wear."""

from pathlib import Path

import pytest

from conftest import create_director_photo_batch, create_project, preview_batch, wait_batch
from app.prompts import (OUTFIT_BY_ID, OUTFIT_RULE, OUTFITS, RETIRED_STYLE_IDS, STYLE_BY_ID, STYLE_OUTFITS, compose_moment_prompt,
                         director_scene_text, outfit_text)
from test_styling import batch_tasks, detail, make_looks

DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()


def test_every_active_style_has_outfits_and_the_doc_has_their_text():
    assert set(STYLE_OUTFITS) == set(STYLE_BY_ID) - RETIRED_STYLE_IDS
    assert {outfit for outfits in STYLE_OUTFITS.values() for outfit in outfits} == set(OUTFIT_BY_ID)
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


@pytest.mark.parametrize(('style_id', 'new_outfit', 'old_outfit'), [
    ('korean_minimal', 'studio_satin', 'satin_a_line'),
    ('forest_natural', 'forest_lace_veil', 'forest_lace'),
    ('french_vintage', 'city_satin_veil', 'city_light'),
])
def test_new_outfits_are_defaults_but_the_originals_remain_selectable(client_app, style_id, new_outfit, old_outfit):
    client, _app = client_app
    project = create_project(client)
    assert detail(client, project['id'])['style_outfits'][style_id] == new_outfit
    assert STYLE_OUTFITS[style_id] == (new_outfit, old_outfit)
    response = client.put(f"/api/projects/{project['id']}/style-outfits/{style_id}", json={'outfit_id': old_outfit})
    assert response.status_code == 200 and response.json()['style_outfits'][style_id] == old_outfit
    batch = preview_batch(client, project['id'], request_id='original-outfit', style_ids=[style_id])
    wait_batch(client, batch['id'])
    task = batch_tasks(client, batch['id'])[0]
    assert outfit_text(old_outfit) in task['prompt'] and outfit_text(new_outfit) not in task['prompt']
    assert STYLE_BY_ID[style_id]['preview'] in task['prompt']


def test_a_sample_wears_the_chosen_outfit_and_its_set_keeps_it_after_the_choice_changes(client_app):
    client, _ = client_app
    project = create_project(client)
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
