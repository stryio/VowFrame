"""AI director validation and mock behavior tests."""

import re
from pathlib import Path

from app.director import FORBIDDEN_WORDS, SHOT_SIZES, SYSTEM_PROMPT, MockDirector, Moment, _shot_requirement, _user_prompt, validate_moment, validate_moments
from app.prompts import STYLES, director_scene_text
from conftest import create_project, preview_batch, wait_batch
from app.db import Database


def test_director_user_prompt_lists_every_variant_matching_section_7_2():
    scene = director_scene_text('cinematic_light', single_variant=False)
    assert _user_prompt('高级光影', scene, 8, _shot_requirement(8), ['海边踏浪回眸', '沙滩并肩漫步']) == (
        '风格：高级光影\n'
        '服装（摄影团队已确定）：新娘穿缎面婚纱，新郎穿黑色西装。新娘发型：盘成轮廓简洁的光洁低发髻。新郎发型：两侧修短的侧分短发，梳理服帖，带自然光泽。妆容方向：有光泽的底妆，深棕色眼影，浓郁的玫瑰红唇，经得起强烈的明暗光。\n'
        '视觉语言（摄影团队已确定）：光和影是画面的主角：方向明确的光在墙面、地面和两人的衣服上形成清晰的明暗结构或投影图案，两人的面部处在光的亮处，均匀明亮、五官清晰；整组照片都在同一个空旷的室内空间里拍摄：白色的墙面、浅灰色的水泥地面，房间里几乎没有家具；背景简洁，颜色服务于光影；灯具等光源本身不出现在画面里。\n'
        '场景变体（每个镜头选其中一个，把编号填在 scene；整组尽量用到不同的变体）：\n'
        '1. 窗格投影：上午的硬质阳光从房间一侧的窗户射入，在白色墙面、水泥地面和两人的衣服上投出清晰的窗格影子。\n'
        '2. 百叶窗条纹：阳光穿过房间一侧的百叶窗，在白色墙面、水泥地面和两人的衣服上落下斜向的条纹光影。\n'
        '3. 树影墙：阳光从一扇大窗照进房间，窗外树枝和树叶的影子投在白色墙面、水泥地面和两人的衣服上。\n'
        '4. 暗调单光：房间的光线压暗，只有侧前方一扇窗透进一束柔光，让人物从暗部中浮现，身后的墙面和地面逐渐沉入暗部。\n'
        '5. 色彩墙侧光：房间里刷成雾蓝色的一面墙，一束侧光从画面一侧掠过墙面和水泥地面，形成由亮到暗的渐变。\n'
        '需要的镜头数量：8\n'
        '景别要求：' + _shot_requirement(8) + '\n'
        '已拍过的镜头（不要重复）：海边踏浪回眸、沙滩并肩漫步\n\n'
        '请输出 JSON：{"moments": [{"title": "2～12 个汉字的标题", "scene": 所选场景的编号, "faces_clear": true | false, "shot_size": "wide | full | three_quarter | half | close", '
        '"orientation": "portrait | landscape", "bride_facing": "front | left | right", "groom_facing": "front | left | right", "text": "镜头描述，最多 220 个字，以句号结尾"}]}\n'
        'scene 是数字，只有一个场景时填 1。faces_clear：两人面部清晰可见时为 true，背影、剪影、人物很小或被遮挡的氛围镜头为 false。shot_size 含义：wide 远景，full 全身，three_quarter 七分身，half 半身，close 近景。'
        'orientation 含义：portrait 竖版，landscape 横版。'
        'facing 按画面所见判断，left 是脸朝画面左侧，right 是脸朝画面右侧；不是 front 时，text 里要写出「新娘的脸朝画面左侧」这样的一句，方向与 facing 一致。'
    )


def test_preview_reference_mode_keeps_director_in_the_first_variant():
    assert director_scene_text('cinematic_light', single_variant=True) == (
        '服装（摄影团队已确定）：新娘穿缎面婚纱，新郎穿黑色西装。新娘发型：盘成轮廓简洁的光洁低发髻。新郎发型：两侧修短的侧分短发，梳理服帖，带自然光泽。妆容方向：有光泽的底妆，深棕色眼影，浓郁的玫瑰红唇，经得起强烈的明暗光。\n'
        '视觉语言（摄影团队已确定）：光和影是画面的主角：方向明确的光在墙面、地面和两人的衣服上形成清晰的明暗结构或投影图案，两人的面部处在光的亮处，均匀明亮、五官清晰；整组照片都在同一个空旷的室内空间里拍摄：白色的墙面、浅灰色的水泥地面，房间里几乎没有家具；背景简洁，颜色服务于光影；灯具等光源本身不出现在画面里。\n'
        '场景（所有镜头都在这个场景里，scene 填 1）：上午的硬质阳光从房间一侧的窗户射入，在白色墙面、水泥地面和两人的衣服上投出清晰的窗格影子。'
    )


def test_text_length_limit_is_220_characters():
    assert validate_moment(Moment('窗边转圈', 'full', '镜' * 219 + '。', 'portrait')) == []
    assert any('220' in problem for problem in validate_moment(Moment('窗边转圈', 'full', '镜' * 220 + '。', 'portrait')))


def test_mock_director_covers_shots_and_avoids_titles():
    director = MockDirector()
    moments = director.write_moments('海边日落', '海边沙滩。', 8, _shot_requirement(8), ['牵手踏浪'], scene_count=3)
    assert len(moments) == 8
    assert {item.shot_size for item in moments} == {'full', 'three_quarter', 'half', 'close'}
    assert len({item.title for item in moments}) == 8
    assert all(not any(word in item.text for word in FORBIDDEN_WORDS) for item in moments)
    assert {item.scene for item in moments} == {1, 2, 3}


def test_director_output_must_state_each_side_facing_in_the_text():
    stated = Moment('相视浅笑', 'half', '两人面对面站着，新娘的脸朝画面右侧，新郎的脸朝画面左侧。', 'portrait', 'right', 'left', 1)
    assert validate_moments([stated], scene_count=1) == []
    swapped = Moment('相视浅笑', 'half', '两人面对面站着，新娘的脸朝画面左侧，新郎的脸朝画面左侧。', 'portrait', 'right', 'left', 1)
    assert any('bride_facing 是 right' in problem for problem in validate_moments([swapped], scene_count=1))
    unstated = Moment('相视浅笑', 'half', '两人面对面站着，相视浅笑。', 'portrait', 'right', 'front', 1)
    assert any('新娘的脸朝画面右侧' in problem for problem in validate_moments([unstated], scene_count=1))
    front = Moment('相视浅笑', 'half', '新娘的脸朝画面右侧，两人相视浅笑。', 'portrait', 'front', 'front', 1)
    assert any('bride_facing 是 front' in problem for problem in validate_moments([front], scene_count=1))
    # A user may change facing by hand without rewriting the text.
    assert validate_moment(unstated) == []


def test_director_output_needs_a_scene_in_range():
    moment = Moment('窗边转圈', 'full', '两人看向镜头。', 'portrait', 'front', 'front', 3)
    assert validate_moments([moment], scene_count=3) == []
    for scene in (None, 0, 4, True, '2'):
        bad = Moment('窗边转圈', 'full', '两人看向镜头。', 'portrait', 'front', 'front', scene)
        assert any('scene' in problem for problem in validate_moments([bad], scene_count=3))
    # Drafts written before V3.34 have no scene.
    assert validate_moment(Moment('窗边转圈', 'full', '两人看向镜头。', 'portrait')) == []


def test_mock_rewrite_keeps_the_facing_its_text_states():
    text = '65mm 镜头，半身。新娘的脸朝画面右侧，新郎看向镜头。'
    moment = MockDirector().rewrite_shot('海边日落', '海边沙滩。', text, 'close', 'portrait')
    assert (moment.bride_facing, moment.groom_facing) == ('right', 'front')


def test_validation_requires_a_known_orientation():
    assert any('orientation' in problem for problem in validate_moment(Moment('窗边转圈', 'full', '两人看向镜头。', None)))
    assert any('orientation 必须为 portrait' in problem for problem in
               validate_moment(Moment('窗边转圈', 'full', '两人看向镜头。', 'landscape'), target_orientation='portrait'))


def test_validation_requires_valid_facing_values():
    problems = validate_moment(Moment('窗边转圈', 'full', '两人看向镜头。', 'portrait', 'diagonal', 'front'))
    assert any('bride_facing' in problem for problem in problems)


def test_validation_checks_forbidden_words():
    invalid = Moment('墨镜', 'half', '新郎戴着墨镜看向镜头。', 'portrait')
    problems = validate_moment(invalid)
    assert any('禁用词' in problem for problem in problems)


def test_validation_allows_eight_moments_with_the_same_shot_size():
    moments = [Moment(f'动作{i}', 'full', '两人一起看向镜头。', 'portrait') for i in range(8)]
    assert validate_moments(moments, count=8) == []


def test_shot_requirement_matches_section_7_2():
    assert _shot_requirement(1) == '不限，选择最适合这个场景的景别和机位。'
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
    assert _shot_requirement(12) == re.search(r'12 张时的原文：\n\n```text\n(.*?)\n```', doc, re.S).group(1)


def test_wedding_set_composition_scales_with_the_count():
    # (standard, interaction, costume, atmosphere) per docs/prompts.md 7.2
    for count, (standard, interaction, costume, atmosphere) in {4: (2, 2, 0, 0), 8: (3, 3, 1, 1), 12: (4, 5, 2, 1), 20: (7, 8, 3, 2)}.items():
        text = _shot_requirement(count)
        assert f'标准照 {standard} 张' in text and f'互动 {interaction} 张' in text
        assert (f'婚纱表现 {costume} 张' in text) == bool(costume)
        assert (f'氛围镜头 {atmosphere} 张' in text) if atmosphere else '这组不安排氛围镜头' in text


def test_style_scene_text_has_no_lens_frame_or_separate_face_light_terms():
    assert all('mm 镜头' not in style['scene_prompt'] for style in STYLES)
    assert all('占画面' not in style['scene_prompt'] for style in STYLES)
    assert all('照亮两人面部' not in style['scene_prompt'] for style in STYLES)
    assert all('照亮两人的面部' not in style['scene_prompt'] for style in STYLES)


def test_draft_and_photo_batch_store_moments(app_factory):
    with app_factory(concurrency=1) as (client, _app):
        project = create_project(client)
        preview = preview_batch(client, project['id'], style_ids=['seaside_sunset'])
        wait_batch(client, preview['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        draft = client.post(f"/api/projects/{project['id']}/moment-drafts", json={
            'preview_slot_id': slot['id'], 'count': 4,
        })
        assert draft.status_code == 201, draft.text
        batch = client.post(f"/api/projects/{project['id']}/photo-batches", json={
            'client_request_id': 'director-batch', 'draft_id': draft.json()['id'],
        })
        assert batch.status_code == 201, batch.text
        wait_batch(client, batch.json()['id'])
        detail = client.get(f"/api/projects/{project['id']}").json()
        photos = [photo for photo in detail['photos'] if photo['batch_id'] == batch.json()['id']]
        assert len(photos) == 4
        assert all(photo['pose_id'] is None and photo['shot_size'] for photo in photos)
        tasks = client.get('/api/tasks', params={'batch_id': batch.json()['id']}).json()
        assert all(task['moment_title'] and task['moment_shot_size'] for task in tasks)
        assert all(task['prompt'].split('\n\n')[3].startswith('镜头：') for task in tasks)
        # Each shot carries the original text of the scene the director chose.
        variants = next(style['variants'] for style in STYLES if style['id'] == 'seaside_sunset')
        scenes = [moment['scene'] for moment in draft.json()['moments']]
        assert sorted(task['prompt'].split('\n\n')[2].split('场景：', 1)[1] for task in tasks) == \
            sorted(variants[scene - 1][1] for scene in scenes)


def test_generation_task_migration_adds_moment_columns_and_change_shot(tmp_path):
    database = Database(tmp_path / 'legacy.db')
    database.initialize()
    with database.connect(write=True) as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('ALTER TABLE generation_tasks RENAME TO generation_tasks_current')
        conn.execute('''CREATE TABLE generation_tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, batch_id TEXT NOT NULL, target_slot_id TEXT NOT NULL,
            status TEXT NOT NULL, reference_image_ids TEXT NOT NULL, style_id TEXT NOT NULL, pose_id TEXT,
            prompt TEXT NOT NULL, model_config_snapshot TEXT NOT NULL, provider_task_id TEXT, output_asset_id TEXT,
            error_code TEXT, error_message TEXT, auto_retry_count INTEGER NOT NULL DEFAULT 0,
            retry_of_task_id TEXT, regenerate_mode TEXT CHECK(regenerate_mode IS NULL OR regenerate_mode IN ('same','identity_boost','change_pose')),
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)''')
        conn.execute('''INSERT INTO generation_tasks SELECT id,project_id,batch_id,target_slot_id,status,reference_image_ids,
            style_id,pose_id,prompt,model_config_snapshot,provider_task_id,output_asset_id,error_code,error_message,
            auto_retry_count,retry_of_task_id,regenerate_mode,created_at,started_at,finished_at
            FROM generation_tasks_current''')
        conn.execute('DROP TABLE generation_tasks_current')
    database.initialize()
    with database.connect() as conn:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(generation_tasks)')}
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='generation_tasks'").fetchone()['sql']
    assert {'moment_title', 'moment_shot_size'} <= columns
    assert 'change_shot' in sql


def test_director_setting_persists_and_draft_is_single_use(app_factory):
    with app_factory(concurrency=1) as (client, _app):
        assert client.patch('/api/settings', json={'director_model': 'director-test'}).json()['director_model'] == 'director-test'
        project = create_project(client)
        preview = preview_batch(client, project['id'], style_ids=['seaside_sunset'])
        wait_batch(client, preview['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        draft = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': slot['id'], 'count': 4}).json()
        batch = client.post(f"/api/projects/{project['id']}/photo-batches", json={'client_request_id': 'once', 'draft_id': draft['id']})
        assert batch.status_code == 201
        reused = client.post(f"/api/projects/{project['id']}/photo-batches", json={'client_request_id': 'twice', 'draft_id': draft['id']})
        assert reused.status_code == 409 and reused.json()['detail']['code'] == 'draft_used'


def test_change_shot_creates_director_version(app_factory):
    with app_factory(concurrency=1) as (client, _app):
        project = create_project(client)
        preview = preview_batch(client, project['id'], style_ids=['seaside_sunset'])
        wait_batch(client, preview['id'])
        slot = client.get(f"/api/projects/{project['id']}").json()['previews'][0]
        draft = client.post(f"/api/projects/{project['id']}/moment-drafts", json={'preview_slot_id': slot['id'], 'count': 4}).json()
        batch = client.post(f"/api/projects/{project['id']}/photo-batches", json={'client_request_id': 'shoot', 'draft_id': draft['id']}).json()
        wait_batch(client, batch['id'])
        photo = client.get(f"/api/projects/{project['id']}").json()['photos'][0]
        target = next(size for size in SHOT_SIZES if size != photo['shot_size'])
        changed = client.post(f"/api/slots/{photo['id']}/change-shot", json={'client_request_id': 'shot-change', 'shot_size': target})
        assert changed.status_code == 201, changed.text
        wait_batch(client, changed.json()['id'])
        task = client.get('/api/tasks', params={'batch_id': changed.json()['id']}).json()[0]
        assert task['moment_shot_size'] == target
        assert client.get(f"/api/slots/{photo['id']}/versions").json()[0]['regenerate_mode'] == 'change_shot'


def test_system_prompt_matches_docs_and_no_longer_offers_detail_shots():
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'prompts.md').read_text()
    assert re.search(r'### 7\.1 系统提示词\n\n```text\n(.*?)\n```', doc, re.S).group(1) == SYSTEM_PROMPT
    assert 'detail' not in SHOT_SIZES and '局部特写' not in SYSTEM_PROMPT
