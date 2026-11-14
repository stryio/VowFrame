"""The style art (V7.33): one shared picture per live style of its outfit, worn by no one, in its main scene."""

import json

import pytest

from app.generator import GeneratorError, OutcomeKind
from app.prompts import (HAT_RULE, OUTFIT_BY_ID, STYLE_ART_COMBINE_PROMPT, STYLE_ART_TEXT, STYLE_BY_ID, STYLE_OUTFITS,
                         compose_moment_prompt, compose_preview_prompt, has_style_art, outfit_text, scene_from_prompt,
                         style_art_product_prompt, style_art_scene_prompt, style_art_version, uses_style_art)
from conftest import (ScriptedGenerator, create_director_photo_batch, create_project, preview_batch, wait_batch,
                      wait_until)

STYLE = 'city_story'
OUTFIT = STYLE_OUTFITS[STYLE][0]


@pytest.fixture(autouse=True)
def _style_without_sets(monkeypatch, tmp_path):
    """V7.73: every style in use has sets now; the art made from a generated scene is checked on one without them,
    and without a shipped outfit picture, so all three steps are made."""
    from app import rooms, style_art
    monkeypatch.setitem(rooms.STYLE_ROOMS, STYLE, ())
    monkeypatch.setattr(style_art, 'OUTFIT_IMAGE_DIR', tmp_path)


def art_calls(stylist):
    """The stylist's requests that made style art: (step, images)."""
    steps = {style_art_product_prompt(OUTFIT): 'product', style_art_scene_prompt(STYLE): 'scene',
             STYLE_ART_COMBINE_PROMPT: 'combine'}
    return [(steps[prompt], images) for images, prompt, _ in stylist.calls if prompt in steps]


def shot_tasks(app, project_id):
    with app.state.db.connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM generation_tasks WHERE project_id=? AND regenerate_mode IS NULL ORDER BY created_at,rowid", (project_id,))]


def test_a_shot_prompt_from_the_art_names_the_picture_instead_of_the_clothes_and_the_place():
    prompt = compose_moment_prompt('french_couture', '两人看向镜头。', '3:4', scene=2, side_references=[('bride', 'left')],
                                   styled=True, outfit='french_flower_hat', art=True)
    identity, style = prompt.split('\n\n')[1:3]
    # The art comes after the side look: the fourth picture here.
    assert '第四张图是服饰和场景参考' in identity and '服装和场景按第四张图、光线按本提示词表现' in identity
    # V7.36: a hat outfit says how the hat is worn.
    assert style.startswith('服装与风格：两人穿第四张图中的服饰；新娘不戴耳饰和项链，和造型照一致；不改变造型照的发型（帽子遮住的部分除外）。' + HAT_RULE)
    look, light = STYLE_ART_TEXT['french_couture']
    assert style.endswith(look + '场景：第四张图中的场景；' + light)
    # Neither the outfit's words nor any scene's objects are left; the scene number is not used.
    assert OUTFIT_BY_ID['french_flower_hat'].bride not in prompt and '雕花护墙板' not in style
    assert uses_style_art(prompt) and scene_from_prompt('french_couture', prompt) is None
    assert not uses_style_art(compose_preview_prompt('french_couture', '3:4', styled=True, outfit='french_flower_hat'))


def test_every_live_style_has_art_words_and_the_art_prompts_leave_out_people():
    for style_id, outfits in STYLE_OUTFITS.items():
        if style_id not in STYLE_ART_TEXT:
            continue
        product = style_art_product_prompt(outfits[0])
        assert '不改变造型照的发型' not in product and '没有任何人' in product
        # V7.109: on the looks' light gray, so white sheer gloves and veils keep their edges.
        assert '浅灰色纯色背景' in product and '纯白色背景' not in product
        assert STYLE_BY_ID[style_id]['variants'][0][1] in style_art_scene_prompt(style_id)


def test_the_first_sample_makes_the_art_and_later_shots_reuse_it(app_factory):
    with app_factory(generator=ScriptedGenerator(), style_art='on') as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=[STYLE])
        wait_batch(client, batch['id'])
        stylist = app.state.stylist
        # Three edits: the outfit on white and the empty scene from words, then the two together.
        steps = art_calls(stylist)
        assert [step for step, _ in steps] == ['product', 'scene', 'combine']
        assert steps[0][1] == steps[1][1] == [] and len(steps[2][1]) == 2
        task = shot_tasks(app, project['id'])[0]
        references, snapshot = json.loads(task['reference_image_ids']), json.loads(task['model_config_snapshot'])
        assert snapshot['style_art'] == {'style': STYLE, 'outfit': OUTFIT, 'version': style_art_version(OUTFIT)}
        assert snapshot['reference_slots'] == [['bride', 'front'], ['groom', 'front'], None]
        assert len(references) == 3 and uses_style_art(task['prompt'])
        # V7.41: the two looks go as one picture, so the art is the second.
        assert '第二张图是服饰和场景参考' in task['prompt'] and outfit_text(OUTFIT) not in task['prompt']
        # The art was sent with the shot, as this project's asset of the shared file.
        sent = app.state.generator.calls[0][0]
        assert len(sent) == 2 and 'style_art' in str(sent[1])

        # A set from the sample: the art is ready, so the tasks carry it from the start; nothing is made again.
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        response = create_director_photo_batch(client, project['id'], slot['id'], request_id='set', count=4)
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        assert len(art_calls(stylist)) == 3
        photos = shot_tasks(app, project['id'])[1:]
        assert all(json.loads(item['reference_image_ids'])[-1] == references[2] for item in photos)

        # Another project shares the same art.
        other = create_project(client)
        batch = preview_batch(client, other['id'], request_id='other', style_ids=[STYLE])
        wait_batch(client, batch['id'])
        assert len(art_calls(stylist)) == 3


def test_samples_waiting_for_the_art_share_one_and_a_set_stays_in_one_scene_without_the_sample(app_factory):
    with app_factory(generator=ScriptedGenerator(), style_art='on', concurrency=3) as (client, app):
        client.patch('/api/settings', json={'batch_reference_mode': 'with_preview'})
        projects = [create_project(client) for _ in range(2)]
        batches = [preview_batch(client, project['id'], request_id=f'p{index}', style_ids=[STYLE])
                   for index, project in enumerate(projects)]
        for batch in batches:
            wait_batch(client, batch['id'])
        assert [step for step, _ in art_calls(app.state.stylist)] == ['product', 'scene', 'combine']
        slot = client.get(f"/api/projects/{projects[0]['id']}").json()['previews'][0]
        response = create_director_photo_batch(client, projects[0]['id'], slot['id'], request_id='set', count=4)
        assert response.status_code == 201, response.text
        for task in shot_tasks(app, projects[0]['id'])[1:]:
            snapshot = json.loads(task['model_config_snapshot'])
            # The art replaces the sample as a reference.
            assert len(json.loads(task['reference_image_ids'])) == 3 and snapshot['reference_slots'][-1] is None
            assert '风格参考' not in task['prompt'] and uses_style_art(task['prompt'])


def test_a_failed_art_fails_the_shot_and_the_next_try_keeps_the_finished_steps(app_factory):
    failing = GeneratorError(OutcomeKind.REJECTED, 'rejected')

    class FailCombine(ScriptedGenerator):
        def generate_image(self, images, prompt, model, aspect_ratio, **kwargs):
            if prompt == STYLE_ART_COMBINE_PROMPT and not getattr(self, 'failed', False):
                self.failed = True
                with self.lock:
                    self.calls.append((list(images), prompt, 0))
                raise failing
            return super().generate_image(images, prompt, model, aspect_ratio, **kwargs)

    stylist = FailCombine()
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, style_art='on') as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=[STYLE])
        wait_batch(client, batch['id'], status='needs_attention')
        with app.state.db.connect() as conn:
            art = dict(conn.execute('SELECT * FROM style_arts').fetchone())
        assert art['status'] == 'failed' and art['product_path'] and art['scene_path'] and art['image_path'] is None
        assert app.state.generator.calls == []  # the paid shot was never sent
        # Trying the sample again only redoes the step that failed.
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        retry = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'again', 'mode': 'same'})
        assert retry.status_code == 201, retry.text
        wait_batch(client, retry.json()['id'])
        assert [step for step, _ in art_calls(stylist)] == ['product', 'scene', 'combine', 'combine']


def test_the_art_page_lists_live_styles_and_making_it_again_replaces_it_for_new_shots(app_factory):
    with app_factory(generator=ScriptedGenerator(), style_art='on') as (client, app):
        arts = client.get('/api/style-arts').json()
        assert {item['style_id'] for item in arts} == {style_id for style_id in STYLE_ART_TEXT if has_style_art(style_id)}
        entry = next(item for item in arts if item['style_id'] == STYLE)
        assert entry['status'] is None and entry['outfit_id'] == OUTFIT and entry['image_url'] is None

        assert client.post(f'/api/style-arts/{STYLE}/generate').status_code == 202
        first = wait_until(lambda: next(item for item in client.get('/api/style-arts').json() if item['style_id'] == STYLE),
                           lambda item: item['status'] == 'ready')
        assert first['in_use_id'] == first['id']
        for kind in ('product', 'scene', 'image'):
            assert client.get(first[f'{kind}_url']).status_code == 200
        assert client.post(f'/api/style-arts/{STYLE}/generate').status_code == 202
        second = wait_until(lambda: next(item for item in client.get('/api/style-arts').json() if item['style_id'] == STYLE),
                            lambda item: item['status'] == 'ready' and item['id'] != first['id'])
        assert second['in_use_id'] == second['id']
        assert len(art_calls(app.state.stylist)) == 6  # made again from scratch, not from the first one's steps
        assert client.post('/api/style-arts/retired_or_unknown/generate').status_code == 404


def test_with_the_setting_off_shots_are_made_as_before(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        assert client.get('/api/settings').json()['style_art_mode'] == 'off'
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=[STYLE])
        wait_batch(client, batch['id'])
        task = shot_tasks(app, project['id'])[0]
        assert 'style_art' not in json.loads(task['model_config_snapshot']) and not uses_style_art(task['prompt'])
        assert art_calls(app.state.stylist) == []
        assert client.patch('/api/settings', json={'style_art_mode': 'on'}).json()['style_art_mode'] == 'on'


def test_an_art_being_made_when_the_backend_stops_is_marked_interrupted(app_factory, tmp_path):
    data_dir = tmp_path / 'restart'
    with app_factory(data_dir=data_dir) as (_client, app):
        with app.state.db.connect(write=True) as conn:
            conn.execute("INSERT INTO style_arts (id,style_id,outfit_id,art_version,status,model,created_at,aspect_ratio) "
                         "VALUES ('a1',?,?,?,'running','gpt-image-2','2026-11-04T00:00:00+00:00','3:2')", (STYLE, OUTFIT, style_art_version(OUTFIT)))
    with app_factory(data_dir=data_dir) as (client, _app):
        entry = next(item for item in client.get('/api/style-arts').json() if item['style_id'] == STYLE)
        assert entry['status'] == 'interrupted' and '可能已经扣费' in entry['error_message']


def test_boosting_likeness_makes_the_new_shot_from_the_art_and_same_replays_the_old_one(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=[STYLE])
        wait_batch(client, batch['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        # Made before the setting was turned on: replaying it keeps its two references.
        app.state.db.set_style_art_mode('on')
        same = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'same', 'mode': 'same'})
        wait_batch(client, same.json()['id'])
        boost = client.post(f"/api/slots/{slot['id']}/regenerate", json={'client_request_id': 'boost', 'mode': 'identity_boost'})
        assert boost.status_code == 201, boost.text
        wait_batch(client, boost.json()['id'])
        with app.state.db.connect() as conn:
            replay, boosted = [dict(row) for row in conn.execute(
                "SELECT * FROM generation_tasks WHERE regenerate_mode IN ('same','identity_boost') ORDER BY created_at")]
        first = shot_tasks(app, project['id'])[0]
        assert replay['reference_image_ids'] == first['reference_image_ids'] and not uses_style_art(replay['prompt'])
        assert len(json.loads(boosted['reference_image_ids'])) == 3 and uses_style_art(boosted['prompt'])
        assert '本次是加强长相的重新生成' in boosted['prompt']
