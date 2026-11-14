"""V7.48: a clearly visible face turns only to an angle its person has a photo of; each direction may have a half
side and a full side (profile) photo."""
from app.director import Moment, validate_moment, validate_moments
from app.facings import available_facings, facing_phrase, uploaded_side
from app.prompts import compose_look_prompt, pair_looks
from conftest import PNG, ScriptedGenerator, create_director_photo_batch, create_project, preview_batch, wait_batch, wait_until
from test_styling import detail, preview_slot

ALLOWED = {'bride': ('front', 'left_full'), 'groom': ('front', 'right')}


def moment(bride='front', groom='front', shot='half', faces_clear=True, text='85mm 镜头，半身。两人相视而笑。'):
    return Moment('相视而笑', shot, text, 'portrait', bride, groom, 1, faces_clear)


def test_a_clear_face_may_only_turn_to_an_angle_with_a_photo():
    assert validate_moment(moment(), allowed_facings=ALLOWED) == []
    assert validate_moment(moment('left_full', 'right', text='85mm 镜头，半身。新娘的脸朝画面左侧，正侧面，新郎的脸朝画面右侧。'),
                           check_facing_text=True, allowed_facings=ALLOWED) == []
    # The bride has no half side photo to the left, the groom none to the left at all.
    problems = validate_moment(moment('left', 'left', text='85mm 镜头，半身。新娘的脸朝画面左侧，新郎的脸朝画面左侧。'),
                               check_facing_text=True, allowed_facings=ALLOWED)
    assert len(problems) == 2 and all('没有这个角度的照片' in problem for problem in problems)
    # Back views, silhouettes and wide shots, where the face is small or hidden, turn freely.
    assert validate_moment(moment('right', shot='half', faces_clear=False), allowed_facings=ALLOWED) == []
    assert validate_moment(moment('right', shot='wide'), allowed_facings=ALLOWED) == []
    # Without a list (a hand edit) nothing is limited.
    assert validate_moment(moment('right'), allowed_facings=None) == []


def test_the_text_says_whether_a_face_is_a_full_side():
    half_written = moment('left_full', text='85mm 镜头，半身。新娘的脸朝画面左侧。')
    assert any('新娘的脸朝画面左侧，正侧面' in problem for problem in validate_moment(half_written, check_facing_text=True))
    full_written = moment('left', text='85mm 镜头，半身。新娘的脸朝画面左侧，正侧面。')
    assert validate_moment(full_written, check_facing_text=True) != []
    assert validate_moments([moment('right_full', text='85mm 镜头，半身。新娘的脸朝画面右侧，正侧面。')]) == []


def test_side_photos_are_picked_by_angle_and_fall_back_within_their_direction():
    project = {'person_a_left_image_id': 'half', 'person_a_left_full_image_id': None,
               'person_a_right_image_id': None, 'person_a_right_full_image_id': 'profile', 'person_a_image_id': 'front'}
    assert available_facings(project, 'a') == ('front', 'left', 'right_full')
    assert uploaded_side(project, 'a', 'left') == ('half', 'left')
    assert uploaded_side(project, 'a', 'left_full') == ('half', 'left')  # a free shot gets the nearest angle
    assert uploaded_side(project, 'a', 'right') == ('profile', 'right_full')
    project['person_a_right_full_image_id'] = None
    assert uploaded_side(project, 'a', 'right') == (None, None)


def test_a_full_side_is_named_in_the_side_look_and_shot_prompts():
    assert facing_phrase('right_full') == '脸朝画面右侧（正侧面）'
    side = compose_look_prompt('groom', 'keep', 'natural', facing='left_full')
    assert side.startswith('这张图是要修的照片，新郎脸朝画面左侧（正侧面）。')
    from app.prompts import compose_moment_prompt
    shot = compose_moment_prompt('city_story', '85mm 镜头，半身。新娘的脸朝画面左侧，正侧面，新郎的脸朝画面右侧。', '3:4',
                                 side_references=[('bride', 'left_full'), ('groom', 'right')], styled=True)
    assert '第三张图是新娘脸朝画面左侧（正侧面）时的造型照' in shot
    assert '第二张图是两人的侧脸造型照：左边是新娘脸朝画面左侧（正侧面）时，右边是新郎脸朝画面右侧时' in pair_looks(shot)


def test_a_full_side_photo_makes_its_own_side_look_and_the_director_may_use_it(app_factory):
    stylist = ScriptedGenerator()
    with app_factory(generator=ScriptedGenerator(), stylist=stylist, concurrency=1) as (client, app):
        project = create_project(client)
        for angle in ('left', 'left_full'):
            response = client.post(f"/api/projects/{project['id']}/images", data={'person': 'b', 'angle': angle},
                                   files={'file': (f'b-{angle}.png', PNG, 'image/png')})
            assert response.status_code == 200, response.text
        person_b = detail(client, project['id'])['person_b']
        assert person_b['left_asset_id'] and person_b['left_full_asset_id'] and person_b['left_full_asset_id'] != person_b['left_asset_id']
        wait_batch(client, preview_batch(client, project['id'], style_ids=['city_story'])['id'])
        sample = preview_slot(client, project['id'])
        assert client.post(f"/api/looks/{sample['look_ids']['groom']}/side-looks").status_code == 201
        sides = wait_until(lambda: detail(client, project['id'])['side_looks'],
                           lambda sides: len(sides) == 2 and all(side['status'] == 'ready' for side in sides))
        assert sorted((side['angle'], side['facing']) for side in sides) == [('left', 'left'), ('left', 'left_full')]
        full = next(side for side in sides if side['facing'] == 'left_full')
        assert full['source_asset_id'] == person_b['left_full_asset_id']
        assert any('新郎脸朝画面左侧（正侧面）' in call[1] for call in stylist.calls)
        # The mock director turns the bride (no side photo) to the camera and keeps the groom's half side.
        wait_batch(client, create_director_photo_batch(client, project['id'], sample['id'], request_id='set').json()['id'])
        with app.state.db.connect() as conn:
            prompts = [row[0] for row in conn.execute('SELECT prompt FROM generation_tasks WHERE moment_title IS NOT NULL')]
        assert prompts and not any('新娘的脸朝画面' in prompt for prompt in prompts)
        # The photo can be removed again.
        assert client.delete(f"/api/projects/{project['id']}/images/b/left_full").status_code == 204
        assert detail(client, project['id'])['person_b']['left_full_asset_id'] is None
