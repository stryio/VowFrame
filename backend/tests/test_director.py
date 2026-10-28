"""AI director validation and mock behavior tests."""

import re
from pathlib import Path

from app.director import FORBIDDEN_WORDS, SHOT_SIZES, SYSTEM_PROMPT, MockDirector, Moment, _shot_requirement, _user_prompt, validate_moment, validate_moments
from app.prompts import STYLES, director_scene_text
from conftest import create_project, preview_batch, wait_batch
from app.db import Database


def test_director_user_prompt_lists_every_variant_matching_section_7_2():
    scene = director_scene_text('seaside_sunset', single_variant=False)
    assert _user_prompt('海边日落', scene, 8, _shot_requirement(8), ['海边踏浪回眸', '沙滩并肩漫步']) == (
        '风格：海边日落\n'
        '视觉语言（摄影团队已确定）：温暖浪漫的黄金时刻，暖橙与粉紫的天空色彩，夕阳形成暖色轮廓光，天空的余晖作为柔和的正面光。\n'
        '场景变体（每个镜头选其中一个，并在镜头里写清背景和光影；整组尽量用到不同的变体）：\n'
        '1. 浪花沙滩：湿润反光的沙滩和涌上来的浪花，夕阳低垂在海平面上方，沙滩上倒映着晚霞。\n'
        '2. 海边礁石：一段深色的礁石，海浪拍在石头上溅起水花，夕阳从侧面照来。\n'
        '3. 海上栈道：一条伸向海面的木栈道，两侧是海水，晚霞倒映在水面上。\n'
        '4. 芒草沙丘：海边长着芒草的沙丘，草叶在风里摆动，在逆光下发着金光。\n'
        '需要的镜头数量：8\n'
        '景别要求：整组要像一次真实的拍摄：有交代环境的远景或全景，有互动的中景，有情绪的近景或特写；'
        '景别、机位和构图都要有变化，不要每张都是两人并排面对镜头。\n'
        '已拍过的镜头（不要重复）：海边踏浪回眸、沙滩并肩漫步\n\n'
        '请输出 JSON：{"moments": [{"title": "2～12 个汉字的标题", "shot_size": "wide | full | medium | close | extreme_close", '
        '"orientation": "portrait | landscape", "text": "镜头描述，最多 220 个字，以句号结尾"}]}\n'
        'shot_size 含义：wide 远景（人物很小，环境为主），full 全景（全身入画），medium 中景（膝盖或腰部以上），'
        'close 近景（胸口以上），extreme_close 特写（只拍面部）。'
        'orientation 含义：portrait 竖版，landscape 横版。'
    )


def test_preview_reference_mode_keeps_director_in_the_first_variant():
    assert director_scene_text('seaside_sunset', single_variant=True) == (
        '视觉语言（摄影团队已确定）：温暖浪漫的黄金时刻，暖橙与粉紫的天空色彩，夕阳形成暖色轮廓光，天空的余晖作为柔和的正面光。\n'
        '场景（所有镜头都在这个场景里）：湿润反光的沙滩和涌上来的浪花，夕阳低垂在海平面上方，沙滩上倒映着晚霞。'
    )


def test_text_length_limit_is_220_characters():
    assert validate_moment(Moment('窗边转圈', 'full', '镜' * 219 + '。', 'portrait')) == []
    assert any('220' in problem for problem in validate_moment(Moment('窗边转圈', 'full', '镜' * 220 + '。', 'portrait')))


def test_mock_director_covers_shots_and_avoids_titles():
    director = MockDirector()
    moments = director.write_moments('海边日落', '海边沙滩。', 8, _shot_requirement(8), ['牵手踏浪'])
    assert len(moments) == 8
    assert {item.shot_size for item in moments} == {'full', 'medium', 'extreme_close', 'close'}
    assert len({item.title for item in moments}) == 8
    assert all(not any(word in item.text for word in FORBIDDEN_WORDS) for item in moments)


def test_validation_requires_a_known_orientation():
    assert any('orientation' in problem for problem in validate_moment(Moment('窗边转圈', 'full', '两人看向镜头。', None)))
    assert any('orientation 必须为 portrait' in problem for problem in
               validate_moment(Moment('窗边转圈', 'full', '两人看向镜头。', 'landscape'), target_orientation='portrait'))


def test_validation_checks_forbidden_words():
    invalid = Moment('背影', 'half', '两人背对镜头形成背影。', 'portrait')
    problems = validate_moment(invalid)
    assert any('禁用词' in problem for problem in problems)


def test_validation_allows_eight_moments_with_the_same_shot_size():
    moments = [Moment(f'动作{i}', 'full', '两人一起看向镜头。', 'portrait') for i in range(8)]
    assert validate_moments(moments, count=8) == []


def test_shot_requirement_matches_section_7_2():
    assert _shot_requirement(1) == '不限，选择最适合这个场景的景别和机位。'
    assert _shot_requirement(8) == (
        '整组要像一次真实的拍摄：有交代环境的远景或全景，有互动的中景，有情绪的近景或特写；'
        '景别、机位和构图都要有变化，不要每张都是两人并排面对镜头。'
    )


def test_style_scene_text_has_no_lens_frame_or_separate_face_light_terms():
    assert all('mm 镜头' not in style['scene_prompt'] for style in STYLES)
    assert all('占画面' not in style['scene_prompt'] for style in STYLES)
    assert all('照亮两人面部' not in style['scene_prompt'] for style in STYLES)


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
        target = next(size for size in ('wide', 'full', 'medium', 'close', 'extreme_close') if size != photo['shot_size'])
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
