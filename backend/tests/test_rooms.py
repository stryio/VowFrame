"""Rooms (V7.34): a style shot in a room the user picks, its set spread over the room's areas."""

import json
from pathlib import Path

from PIL import Image

from app.prompts import (LIGHTING_RULES, STYLE_BY_ID, STYLE_ART_COMBINE_PROMPT, STYLE_ART_GRAY_PROMPT, STYLE_OUTFITS, style_art_version,
                         compose_moment_prompt, compose_preview_prompt,
                         director_scene_count, director_scene_text, style_art_product_prompt, style_art_scene_prompt,
                         uses_style_art)
from app.rooms import STYLE_ROOMS, default_room, get_room, main_image, style_rooms
from conftest import PNG, ScriptedGenerator, create_director_photo_batch, create_project, ensure_looks, preview_batch, wait_batch, wait_until

DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text(encoding='utf-8')
STYLE = 'french_couture'
OUTFIT = STYLE_OUTFITS[STYLE][0]
ROOM = 'piano_corner'


def tasks_of(app, project_id, where='1=1'):
    with app.state.db.connect() as conn:
        return [dict(row) for row in conn.execute(
            f'SELECT * FROM generation_tasks WHERE project_id=? AND {where} ORDER BY created_at,rowid', (project_id,))]


def snapshot_of(task):
    return json.loads(task['model_config_snapshot'])


def test_every_area_has_its_picture_and_its_documented_words():
    for style_id, rooms in STYLE_ROOMS.items():
        assert len({room.id for room in rooms}) == len(rooms)
        for room in rooms:
            assert room.areas and f'**{room.name}** `{room.id}`' in DOC
            # V7.50: the main picture is the room's only picture; it shows every area.
            with Image.open(main_image(style_id, room.id)) as picture:
                if room.id in ('tall_window', 'piano_corner', 'painting_wall'):
                    assert picture.size == (4096, 2304)  # V7.44: 16:9 4K
                else:  # V7.53: backdrops made since are 3:2; V7.59: 2K from then on
                    assert picture.width >= 2048 and abs(picture.width / picture.height - 1.5) < 0.01
            for number, area in enumerate(room.areas, 1):
                assert f'{number}. {area.name} `{area.id}`：{area.text}' in DOC and f'光线：{area.light}' in DOC
                assert area.text.endswith('。') and area.light.endswith('。')


def test_a_shot_in_a_room_names_its_area_and_keeps_the_area_light():
    room = get_room(STYLE, ROOM)
    with_art = compose_moment_prompt(STYLE, '两人看向镜头。', '3:4', scene=2, styled=True, outfit=OUTFIT, art=True, room=ROOM)
    style = with_art.split('\n\n')[2]
    # V7.50: the art shows the whole room; another area walks to that part of it.
    assert style.endswith(f'场景：第三张图中的场景，这一张拍在{room.name}的{room.areas[1].name}；参考图拍的是整个房间，'
                          f'这一张走到图中{room.areas[1].name}那里拍，机位、角度和取景按镜头描述，不照搬参考图的取景；陈设只用图里有的。'
                          + room.areas[1].light)
    main = compose_moment_prompt(STYLE, '两人看向镜头。', '3:4', scene=1, styled=True, outfit=OUTFIT, art=True, room=ROOM)
    assert '参考图拍的是整个房间' not in main
    assert room.areas[1].text not in with_art and uses_style_art(with_art)
    words = compose_moment_prompt(STYLE, '两人看向镜头。', '3:4', scene=3, styled=True, outfit=OUTFIT, room=ROOM)
    assert words.split('\n\n')[2].endswith('场景：' + room.areas[2].text + room.areas[2].light)
    # A sample is shot in the room's main picture.
    sample = compose_preview_prompt(STYLE, '3:4', styled=True, outfit=OUTFIT, room=ROOM)
    assert '场景：' + room.areas[0].text in sample


def test_the_director_gets_every_area_of_the_room_or_only_the_first_with_the_sample():
    room = get_room(STYLE, ROOM)
    text = director_scene_text(STYLE, single_variant=False, outfit=OUTFIT, room=ROOM)
    assert f'「{room.name}」这一个房间' in text
    assert all(f'{number}. {area.name}：{area.text}{area.light}' in text for number, area in enumerate(room.areas, 1))
    assert director_scene_count(STYLE, single_variant=False, room=ROOM) == len(room.areas)
    single = director_scene_text(STYLE, single_variant=True, outfit=OUTFIT, room=ROOM)
    assert room.areas[0].text in single and room.areas[1].text not in single
    assert director_scene_count(STYLE, single_variant=True, room=ROOM) == 1


def test_samples_share_the_director_shot_pipeline_and_lighting_in_every_current_set():
    for style_id in STYLE_ROOMS:
        shot = STYLE_BY_ID[style_id]['preview'].removeprefix('镜头：')
        for room in style_rooms(style_id):
            for art in (False, True):
                options = dict(styled=True, outfit=STYLE_OUTFITS[style_id][0], room=room.id, art=art)
                sample = compose_preview_prompt(style_id, '3:4', **options)
                photo = compose_moment_prompt(style_id, shot, '3:4', scene=1, **options)
                assert sample == photo
                paragraphs = sample.split('\n\n')
                assert room.areas[0].light in paragraphs[2]
                assert '两人的脸正对镜头' in paragraphs[3]
                assert LIGHTING_RULES in paragraphs[4]
                assert '侧后方有一点亮边' not in paragraphs[3]
                assert '明亮的阳光从侧前方' not in paragraphs[3]
                # Another area has the same rules but its own light, without the sample's frontal pose.
                if len(room.areas) > 1:
                    other = compose_moment_prompt(style_id, '两人在场景中相视而笑。', '3:4', scene=2,
                                                   **options).split('\n\n')
                    assert room.areas[1].light in other[2] and LIGHTING_RULES in other[4]
                    assert '两人的脸正对镜头' not in other[3]


def test_creating_a_sample_does_not_request_the_director(app_factory, monkeypatch):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        def forbid_director(*args, **kwargs):
            raise AssertionError('A prewritten sample must not request the director')

        monkeypatch.setattr(app.state.director, 'write_moments', forbid_director)
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=[STYLE])
        wait_batch(client, batch['id'])
        sample = tasks_of(app, project['id'])[-1]
        assert '两人的脸正对镜头' in sample['prompt'].split('\n\n')[3]
        assert LIGHTING_RULES in sample['prompt']


def test_a_backdrop_is_a_room_of_one_area_that_the_director_reads_as_one_scene():
    # V7.53: 主纱系列 is shot in front of one of six backdrops, three plain colours and three of light and shadow.
    backdrops = STYLE_ROOMS['minimal_bridal']
    assert [room.id for room in backdrops] == ['light_gray', 'mid_gray', 'charcoal', 'satin_light', 'window_beam', 'water_ripple']
    assert all(len(room.areas) == 1 for room in backdrops)
    room = backdrops[3]
    text = director_scene_text('minimal_bridal', single_variant=False, room=room.id)
    assert '场景（所有镜头都在这个场景里，scene 填 1）：' + room.areas[0].text + room.areas[0].light in text
    assert '几个区域都要用到' not in text
    assert director_scene_count('minimal_bridal', single_variant=False, room=room.id) == 1
    shot = compose_moment_prompt('minimal_bridal', '两人看向镜头。', '3:4', scene=1, styled=True,
                                 outfit=STYLE_OUTFITS['minimal_bridal'][0], art=True, room=room.id)
    assert f'这一张拍在{room.name}的{room.areas[0].name}；' + room.areas[0].light in shot and '浅灰' not in shot


def test_a_sample_is_shot_in_the_picked_room_and_its_set_over_the_room_areas(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        styles = {style['id']: style for style in client.get('/api/styles').json()}
        assert [room['id'] for room in styles[STYLE]['rooms']] == ['piano_corner', 'green_wall', 'wood_door', 'tall_window', 'tea_table']  # V7.77, V7.107
        assert styles['korean_minimal']['rooms'] == []
        piano = next(room for room in styles[STYLE]['rooms'] if room['id'] == ROOM)
        # The main picture is there; another area has no picture until a shot needs it (V7.38).
        assert client.get(piano['areas'][0]['image_url']).status_code == 200 and piano['areas'][1]['image_url'] is None
        assert client.get(f'/api/styles/{STYLE}/rooms/{ROOM}/areas/2/image').status_code == 404

        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], request_id='warm-up', style_ids=[STYLE])['id'])  # makes the looks
        bad = client.post(f"/api/projects/{project['id']}/previews",
                          json={'client_request_id': 'bad', 'style_ids': [STYLE], 'rooms': {STYLE: 'nowhere'}})
        assert bad.status_code == 422 and bad.json()['detail']['code'] == 'invalid_room'
        response = client.post(f"/api/projects/{project['id']}/previews",
                               json={'client_request_id': 'room', 'style_ids': [STYLE], 'rooms': {STYLE: ROOM}})
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        sample = tasks_of(app, project['id'], "regenerate_mode IS NULL AND moment_title IS NULL")[-1]
        assert snapshot_of(sample)['room'] == {'id': ROOM, 'area': 1}
        assert get_room(STYLE, ROOM).areas[0].text in sample['prompt']
        # V7.56: the warm-up sample (the first room) stays; the picked room has a sample of its own after it.
        previews = client.get(f"/api/projects/{project['id']}").json()['previews']
        assert default_room(STYLE) == ROOM and [preview['room']['id'] for preview in previews] == [ROOM]
        slot = previews[0]
        assert slot['room'] == {'id': ROOM, 'name': '钢琴角', 'area': 1, 'area_name': '钢琴前'}

        response = create_director_photo_batch(client, project['id'], slot['id'], request_id='set', count=4)
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        photos = tasks_of(app, project['id'], 'moment_title IS NOT NULL')
        areas = [snapshot_of(task)['room'] for task in photos]
        assert {area['id'] for area in areas} == {ROOM} and {area['area'] for area in areas} == {1, 2, 3}
        for task, area in zip(photos, areas):
            assert get_room(STYLE, ROOM).areas[area['area'] - 1].text in task['prompt']

        # Boosting likeness keeps the shot's area.
        photo_slot = next(item for item in client.get(f"/api/projects/{project['id']}").json()['photos']
                          if item['room']['area'] == 2)
        boost = client.post(f"/api/slots/{photo_slot['id']}/regenerate", json={'client_request_id': 'boost', 'mode': 'identity_boost'})
        assert boost.status_code == 201, boost.text
        boosted = tasks_of(app, project['id'], "regenerate_mode='identity_boost'")[0]
        assert snapshot_of(boosted)['room'] == {'id': ROOM, 'area': 2}


def test_a_sample_without_a_pick_takes_the_first_room(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        batch = preview_batch(client, project['id'], style_ids=[STYLE])
        wait_batch(client, batch['id'])
        sample = tasks_of(app, project['id'])[-1]
        assert snapshot_of(sample)['room'] == {'id': default_room(STYLE), 'area': 1}


def art_steps(stylist):
    names = {style_art_product_prompt(OUTFIT): 'product', STYLE_ART_GRAY_PROMPT: 'gray', style_art_scene_prompt(STYLE): 'scene',
             STYLE_ART_COMBINE_PROMPT: 'combine'}
    return [names[prompt] for _images, prompt, _ in stylist.calls if prompt in names]


def test_restored_tall_window_is_selectable_and_its_sample_uses_the_existing_reference(app_factory):
    with app_factory(generator=ScriptedGenerator(), style_art='on') as (client, app):
        style = next(item for item in client.get('/api/styles').json() if item['id'] == STYLE)
        room = next(item for item in style['rooms'] if item['id'] == 'tall_window')
        assert room['name'] == '高窗纱帘' and len(room['areas']) == 3
        assert not get_room(STYLE, 'tall_window').retired
        assert default_room(STYLE) == ROOM
        assert get_room(STYLE, 'painting_wall').retired
        image = client.get(room['areas'][0]['image_url'])
        assert image.status_code == 200
        assert image.content == main_image(STYLE, 'tall_window').read_bytes()

        project = create_project(client)
        ensure_looks(client, project['id'], [STYLE])
        response = client.post(f"/api/projects/{project['id']}/previews", json={
            'client_request_id': 'restored-window', 'style_ids': [STYLE], 'rooms': {STYLE: ['tall_window']},
        })
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        previews = client.get(f"/api/projects/{project['id']}").json()['previews']
        assert len(previews) == 1
        assert previews[0]['room'] == {'id': 'tall_window', 'name': '高窗纱帘', 'area': 1, 'area_name': '高窗前'}
        task = tasks_of(app, project['id'], "regenerate_mode IS NULL AND moment_title IS NULL")[-1]
        snapshot = snapshot_of(task)
        assert snapshot['room'] == {'id': 'tall_window', 'area': 1}
        assert snapshot['style_art']['room'] == 'tall_window'
        assert '高窗纱帘' in task['prompt']
        with app.state.db.connect() as conn:
            art = conn.execute("SELECT * FROM style_arts WHERE room_id='tall_window' AND status='ready'").fetchone()
        assert art is not None
        assert (app.state.config.data_dir / art['scene_path']).read_bytes() == image.content


def test_a_room_has_one_art_of_its_main_picture_and_every_area_uses_it(app_factory):
    with app_factory(generator=ScriptedGenerator(), style_art='on', concurrency=3) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], request_id='looks', style_ids=[STYLE])['id'])
        response = client.post(f"/api/projects/{project['id']}/previews",
                               json={'client_request_id': 'room', 'style_ids': [STYLE], 'rooms': {STYLE: ROOM}})
        wait_batch(client, response.json()['id'])
        slot = next(s for s in client.get(f"/api/projects/{project['id']}").json()['previews'] if s['room']['id'] == ROOM)
        response = create_director_photo_batch(client, project['id'], slot['id'], request_id='set', count=4)
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        steps = art_steps(app.state.stylist)
        # The outfit picture comes from the repository (V7.35; the V7.51 outfits ship theirs), turned gray by one edit
        # (V7.109) and shared by every room; no scene is generated from words and no area picture is made (V7.50); one
        # combination per room (V7.58: the first room is this one, so the warm-up sample's art serves the set too).
        assert 'product' not in steps and 'scene' not in steps and steps.count('gray') == 1 and steps.count('combine') == 1
        with app.state.db.connect() as conn:
            arts = [dict(row) for row in conn.execute("SELECT * FROM style_arts WHERE status='ready'")]
        assert default_room(STYLE) == ROOM and {(art['room_id'], art['area']) for art in arts} == {(ROOM, 1)}
        from app.style_art import outfit_product_image
        assert {(app.state.config.data_dir / art['product_path']).read_bytes() for art in arts} == {outfit_product_image(OUTFIT).read_bytes()}
        # V7.52: every art is 3:2, though the room's main picture is 16:9.
        assert {art['aspect_ratio'] for art in arts} == {'3:2'}
        stylist = app.state.stylist
        combine = [(images, ratio) for (images, prompt, _), ratio in zip(stylist.calls, stylist.aspect_ratios)
                   if prompt == STYLE_ART_COMBINE_PROMPT]
        assert {ratio for _images, ratio in combine} == {'3:2'}
        room_art = next(art for art in arts if art['room_id'] == ROOM)
        assert (app.state.config.data_dir / room_art['scene_path']).read_bytes() == main_image(STYLE, ROOM).read_bytes()
        photos = tasks_of(app, project['id'], 'moment_title IS NOT NULL')
        assert {snapshot_of(task)['room']['area'] for task in photos} == {1, 2, 3}
        for task in photos:
            snapshot = snapshot_of(task)
            assert snapshot['style_art'] == {'style': STYLE, 'outfit': OUTFIT, 'version': style_art_version(OUTFIT), 'room': ROOM, 'area': 1}
            assert '这一张拍在钢琴角的' in task['prompt']
        # Every shot of the room carries the same art.
        assert len({json.loads(task['reference_image_ids'])[-1] for task in photos}) == 1

        listed = [item for item in client.get('/api/style-arts').json() if item['style_id'] == STYLE]
        assert len(listed) == len(style_rooms(STYLE)) and all(item['area'] == 1 for item in listed)  # not the retired ones
        entry = next(item for item in listed if item['room_id'] == ROOM)
        assert entry['status'] == 'ready' and entry['area_name'] == '钢琴前' and client.get(entry['area_image_url']).status_code == 200
        # Making the room's art again redoes its combination only, with the shared outfit picture.
        assert client.post(f'/api/style-arts/{STYLE}/generate').status_code == 422
        assert client.post(f'/api/style-arts/{STYLE}/generate', params={'room': ROOM}).status_code == 202
        wait_until(lambda: next(item for item in client.get('/api/style-arts').json() if item['room_id'] == ROOM),
                   lambda item: item['status'] == 'ready' and item['id'] != entry['id'])
        steps = art_steps(app.state.stylist)
        assert 'product' not in steps and steps.count('gray') == 1 and steps.count('combine') == 2
        # Areas other than the main one have no picture of their own.
        assert client.get(f'/api/styles/{STYLE}/rooms/{ROOM}/areas/2/image').status_code == 404
        assert client.post(f'/api/styles/{STYLE}/rooms/{ROOM}/areas/2/generate').status_code in (404, 405)


def test_an_outfit_with_a_checked_picture_has_it_turned_gray_instead_of_made_from_words(app_factory):
    from app.style_art import outfit_product_image
    outfit = 'french_illusion_satin'
    assert outfit in STYLE_OUTFITS[STYLE] and outfit_product_image(outfit) is not None
    with app_factory(generator=ScriptedGenerator(), style_art='on') as (client, app):
        project = create_project(client)
        assert client.put(f"/api/projects/{project['id']}/style-outfits/{STYLE}", json={'outfit_id': outfit}).status_code == 200
        wait_batch(client, preview_batch(client, project['id'], style_ids=[STYLE])['id'])
        prompts_sent = [prompt for _images, prompt, _ in app.state.stylist.calls]
        assert style_art_product_prompt(outfit) not in prompts_sent and prompts_sent.count(STYLE_ART_COMBINE_PROMPT) == 1
        # V7.109: the checked picture (on white) is the one edit's only image.
        assert [images for images, prompt, _ in app.state.stylist.calls if prompt == STYLE_ART_GRAY_PROMPT] == [[outfit_product_image(outfit)]]
        with app.state.db.connect() as conn:
            art = conn.execute("SELECT * FROM style_arts WHERE status='ready'").fetchone()
        assert art['outfit_id'] == outfit
        assert (app.state.config.data_dir / art['product_path']).read_bytes() == outfit_product_image(outfit).read_bytes()


def test_a_sample_lists_the_arts_of_its_set_and_shows_the_one_it_used(app_factory):
    outfit = 'french_illusion_satin'
    with app_factory(generator=ScriptedGenerator(), style_art='on') as (client, app):
        project = create_project(client)
        client.put(f"/api/projects/{project['id']}/style-outfits/{STYLE}", json={'outfit_id': outfit})
        wait_batch(client, preview_batch(client, project['id'], style_ids=[STYLE])['id'])
        sample = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        assert sample['style_art_asset_id'] and client.get(f"/api/assets/{sample['style_art_asset_id']}").status_code == 200
        arts = client.get(f"/api/slots/{sample['id']}/style-arts").json()
        room = get_room(STYLE, default_room(STYLE))
        # V7.50: one art per room, the one the sample made.
        assert [(item['room_id'], item['area'], item['outfit_id']) for item in arts] == [(room.id, 1, outfit)]
        assert arts[0]['status'] == 'ready' and arts[0]['in_use_id'] == arts[0]['id']
        response = client.post(f'/api/style-arts/{STYLE}/generate', params={'room': room.id, 'outfit': outfit})
        assert response.status_code == 202, response.text
        wait_until(lambda: client.get(f"/api/slots/{sample['id']}/style-arts").json()[0],
                   lambda item: item['status'] == 'ready' and item['id'] != arts[0]['id'])
        assert client.post(f'/api/style-arts/{STYLE}/generate',
                           params={'room': room.id, 'outfit': 'city_light'}).status_code == 422
        # With the setting off, a sample's set uses no art.
        client.patch('/api/settings', json={'style_art_mode': 'off'})
        assert client.get(f"/api/slots/{sample['id']}/style-arts").json() == []


def test_a_hat_outfit_says_how_the_hat_is_worn_and_that_its_crown_hides_the_hair():
    from app.prompts import HAT_RULE, has_hat
    assert HAT_RULE in DOC and has_hat('french_flower_hat') and has_hat('celebration_qipao') and not has_hat('french_illusion_satin')
    art = compose_preview_prompt(STYLE, '3:4', styled=True, outfit='french_flower_hat', art=True, room=ROOM).split('\n\n')[2]
    assert art.startswith('服装与风格：两人穿第三张图中的服饰；新娘不戴耳饰和项链，和造型照一致；不改变造型照的发型（帽子遮住的部分除外）。' + HAT_RULE)
    words = compose_preview_prompt(STYLE, '3:4', styled=True, outfit='french_flower_hat', room=ROOM)
    assert HAT_RULE in words
    plain = compose_preview_prompt(STYLE, '3:4', styled=True, outfit='french_illusion_satin', art=True, room=ROOM)
    assert HAT_RULE not in plain and '不改变造型照的发型。' in plain


def test_a_sample_can_pick_its_outfit_next_to_its_room_and_it_becomes_the_style_choice(app_factory):
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=[STYLE])['id'])
        bad = client.post(f"/api/projects/{project['id']}/previews",
                          json={'client_request_id': 'bad', 'style_ids': [STYLE], 'outfits': {STYLE: 'city_light'}})
        assert bad.status_code == 422 and bad.json()['detail']['code'] == 'invalid_outfit'
        response = client.post(f"/api/projects/{project['id']}/previews", json={
            'client_request_id': 'pick', 'style_ids': [STYLE], 'rooms': {STYLE: ROOM}, 'outfits': {STYLE: 'french_hat_ballgown'}})
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        sample = tasks_of(app, project['id'])[-1]
        assert snapshot_of(sample)['outfit'] == 'french_hat_ballgown' and snapshot_of(sample)['room']['id'] == ROOM
        assert client.get(f"/api/projects/{project['id']}").json()['style_outfits'][STYLE] == 'french_hat_ballgown'


def test_a_shot_with_turned_or_moving_faces_ends_with_a_likeness_reminder():
    from app.prompts import LIKENESS_LABEL, likeness_note, shot_text_from_prompt
    turned = '新娘手扶台沿看他。新娘的脸朝画面左侧。'
    prompt = compose_moment_prompt(STYLE, turned, '3:4', scene=1, styled=True, outfit=OUTFIT, art=True, room=ROOM)
    shot = prompt.split('\n\n')[3]
    assert shot.endswith('长相：新娘的头部角度和表情按镜头变化，五官比例、眼型、眉形、脸型和发际线仍与造型照一致，只改变角度和表情，'
                         '不画成另一张脸。新娘侧过脸时，按造型照推断同一个人的侧面轮廓。')
    assert shot_text_from_prompt(prompt) == turned  # a redo takes the director's text back without the reminder
    assert '两人的头部角度' in likeness_note('两人低头笑开。') and likeness_note('两人看向镜头微笑。') == ''
    assert likeness_note('新娘低头。', faces_clear=False) == ''
    assert LIKENESS_LABEL in DOC and '只改变角度和表情，不画成另一张脸。' in DOC


def test_paired_looks_name_one_picture_for_both_and_move_the_later_pictures_forward():
    from app.prompts import PAIRED_LOOKS_ROLE, pair_looks
    prompt = compose_moment_prompt(STYLE, '两人看向镜头。', '3:4', scene=1, styled=True, outfit=OUTFIT, art=True, room=ROOM,
                                   side_references=[('bride', 'right'), ('groom', 'left')])
    paired = pair_looks(prompt)
    # V7.43: two side looks are one picture too, so the style art moves from the fifth picture to the third.
    assert (PAIRED_LOOKS_ROLE + '，第二张图是两人的侧脸造型照：左边是新娘脸朝画面右侧时，右边是新郎脸朝画面左侧时。'
            '第三张图是服饰和场景参考') in paired
    assert '两人穿第三张图中的服饰' in paired and '第四张' not in paired and '第五张' not in paired
    assert '侧脸造型照只用来参考侧脸时的五官和脸型，头饰、头纱和花冠以第三张图的服饰为准。' in paired
    one = pair_looks(compose_moment_prompt(STYLE, '两人看向镜头。', '3:4', scene=1, styled=True, outfit=OUTFIT, art=True,
                                           room=ROOM, side_references=[('groom', 'left')]))
    assert PAIRED_LOOKS_ROLE + '，第二张图是新郎脸朝画面左侧时的造型照。第三张图是服饰和场景参考' in one
    assert '花冠以第三张图的服饰为准' in one
    three = pair_looks(compose_moment_prompt(STYLE, '两人看向镜头。', '3:4', scene=1, styled=True, outfit=OUTFIT, art=True,
                                             room=ROOM, side_references=[('bride', 'left'), ('bride', 'right'), ('groom', 'left')]))
    assert ('第二张图是两人的侧脸造型照：从左到右依次是新娘脸朝画面左侧时、新娘脸朝画面右侧时、新郎脸朝画面左侧时。'
            '第三张图是服饰和场景参考') in three
    assert '第三张图是风格参考' not in pair_looks(compose_moment_prompt(STYLE, '两人看向镜头。', '3:4', scene=1, styled=True))
    assert '左边是新娘，右边是新郎' in DOC and '两人的侧脸造型照：左边是' in DOC


def test_two_side_looks_are_sent_as_one_picture_after_the_paired_looks(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator) as (client, app):
        project = create_project(client)
        for person, angle in (('a', 'right'), ('b', 'left')):
            response = client.post(f"/api/projects/{project['id']}/images", data={'person': person, 'angle': angle},
                                   files={'file': (f'{person}-{angle}.png', PNG, 'image/png')})
            assert response.status_code == 200
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        response = create_director_photo_batch(client, project['id'], slot['id'], request_id='sides', count=4)
        assert response.status_code == 201, response.text
        wait_batch(client, response.json()['id'])
        task = tasks_of(app, project['id'], f"batch_id='{response.json()['id']}'")[2]
        assert snapshot_of(task)['paired_sides'] is True and len(json.loads(task['reference_image_ids'])) == 4
        # Mock shot 3 turns the bride right and the groom left: the looks, then both side looks in one picture.
        images = next(call[0] for call in generator.calls if call[1] == task['prompt'])
        assert [path.name for path in images] == [f"{task['id']}-looks.png", f"{task['id']}-sides.png"]
        assert not any(path.exists() for path in images)  # put together for the request only


def test_the_director_sees_the_room_main_picture_but_never_the_sample(app_factory):
    from app.director import _user_content
    with app_factory(generator=ScriptedGenerator(), style_art='on') as (client, app):
        seen = []
        previous = app.state.director

        class Recording:
            def write_moments(self, *args, images=(), **kwargs):
                seen.append([str(path) for _label, path in images])
                return previous.write_moments(*args, **kwargs)

        app.state.director = Recording()
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], style_ids=[STYLE])['id'])
        response = client.post(f"/api/projects/{project['id']}/previews",
                               json={'client_request_id': 'room', 'style_ids': [STYLE], 'rooms': {STYLE: ROOM}})
        wait_batch(client, response.json()['id'])
        slot = next(s for s in client.get(f"/api/projects/{project['id']}").json()['previews'] if s['room']['id'] == ROOM)
        first = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': slot['id'], 'count': 4})
        assert first.status_code == 201, first.text
        # V7.50: the main picture shows every area; the sample is not sent.
        assert seen[-1] == [str(main_image(STYLE, ROOM))]
    parts = _user_content('文字', [('场景 1「钢琴前」的实景图：', main_image(STYLE, ROOM))])
    assert [part['type'] for part in parts] == ['text', 'image_url', 'text'] and parts[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert _user_content('文字') == '文字'


def test_a_style_has_a_sample_per_room_and_a_set_copies_the_sample_of_its_room(app_factory):
    # V7.56 (user: 同一种风格不同场景都弄): several rooms in one request, one sample each; asking again for a room
    # makes a new version of that room's sample, not of another room's.
    with app_factory(generator=ScriptedGenerator()) as (client, app):
        project = create_project(client)
        wait_batch(client, preview_batch(client, project['id'], request_id='warm-up', style_ids=[STYLE])['id'])
        both = client.post(f"/api/projects/{project['id']}/previews",
                           json={'client_request_id': 'both', 'style_ids': [STYLE], 'rooms': {STYLE: ['wood_door', 'green_wall']}})
        assert both.status_code == 201, both.text
        wait_batch(client, both.json()['id'])
        previews = client.get(f"/api/projects/{project['id']}").json()['previews']
        assert [preview['room']['id'] for preview in previews] == ['piano_corner', 'green_wall', 'wood_door']
        again = client.post(f"/api/projects/{project['id']}/previews",
                            json={'client_request_id': 'again', 'style_ids': [STYLE], 'rooms': {STYLE: [ROOM]}})
        wait_batch(client, again.json()['id'])
        after = {preview['room']['id']: preview for preview in client.get(f"/api/projects/{project['id']}").json()['previews']}
        assert len(after) == 3 and after[ROOM]['version_count'] == 2 and after['wood_door']['version_count'] == 1
        # V7.58: a retired room cannot take a new sample.
        for bad in ([], [ROOM, ROOM], [ROOM, 'nowhere'], ['painting_wall']):
            response = client.post(f"/api/projects/{project['id']}/previews",
                                   json={'client_request_id': f'bad-{len(bad)}-{bad[-1] if bad else ""}', 'style_ids': [STYLE], 'rooms': {STYLE: bad}})
            assert response.status_code == 422 and response.json()['detail']['code'] == 'invalid_room'


def test_a_sample_from_before_rooms_per_sample_keeps_the_room_of_its_picture(tmp_path):
    # V7.56: an older database had one sample per style; it gets the room its latest picture was shot in.
    import sqlite3
    from app.db import Database
    db = Database(tmp_path / 'old.db')
    db.initialize()
    with db.connect(write=True) as conn:
        conn.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','p','t','t')")
        conn.execute("INSERT INTO generation_batches (id,project_id,batch_type,client_request_id,created_at) VALUES ('b','p','preview','r','t')")
        conn.execute(f"INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,created_at) VALUES ('s','p','b','preview','{STYLE}','t')")
        conn.execute("INSERT INTO generation_tasks (id,project_id,batch_id,target_slot_id,status,reference_image_ids,style_id,prompt,"
                     f"model_config_snapshot,created_at) VALUES ('t','p','b','s','completed','[]','{STYLE}','x',"
                     f"'{{\"room\": {{\"id\": \"{ROOM}\", \"area\": 1}}}}','t')")
    db.initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT room_id FROM photo_slots WHERE id='s'").fetchone()[0] == ROOM
        conn.execute(f"INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,room_id,created_at) VALUES ('s2','p','b','preview','{STYLE}','tall_window','t')")
        try:
            conn.execute(f"INSERT INTO photo_slots (id,project_id,batch_id,kind,style_id,room_id,created_at) VALUES ('s3','p','b','preview','{STYLE}','{ROOM}','t')")
            raise AssertionError('two samples of one room')
        except sqlite3.IntegrityError:
            pass
