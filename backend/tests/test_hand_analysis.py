"""Vision diagnosis never bills an image edit or silently substitutes generic instructions."""

import base64
import copy
import io
import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from app.config import Config
from app.hand_analysis import ApiHandAnalyzer, HandAnalysisError, HandReport, analysis_inputs
from conftest import ScriptedGenerator, wait_batch
from test_regional_edit import SIZE, annotation, crop_annotation, ready


def finding(**changes):
    region = {
        'label': '画面中央握花的手', 'status': 'repair', 'bbox': [0.12, 0.15, 0.52, 0.52],
        'evidence': ['中央两个指尖共用指甲轮廓'], 'problems': ['中央两指末端粘连'],
        'goal': '分开中央两个指尖及指甲，恢复两指之间的间隙。',
        'preserve': ['握花动作、戒指和花束遮挡'], 'uncertainties': ['花束后的手指不可见'],
    }
    region.update(changes)
    return region


def report(**changes):
    value = {'status': 'repair', 'summary': '握花的手有指尖粘连。', 'regions': [finding()]}
    value.update(changes)
    return value


class ScriptedAnalyzer:
    def __init__(self, result=None, error=None, before_return=None):
        self.result = HandReport.model_validate(report() if result is None else result)
        self.error, self.before_return, self.calls = error, before_return, []

    def analyze(self, parts, *, model, provider, selection=False):
        self.calls.append((parts, model, provider))
        if self.before_return:
            self.before_return()
        if self.error:
            raise self.error
        return self.result


def analyze(client, slot, *, selected=False, kind='crop', content=None, version=None):
    data = {'source_version_id': version or slot['current_version']['id'], 'annotation_kind': kind}
    files = {'annotation': ('selection.png', content or (crop_annotation() if kind == 'crop' else annotation(kind)), 'image/png')} if selected else None
    return client.post(f"/api/slots/{slot['id']}/hand-analysis", data=data, files=files)


@pytest.mark.parametrize('kind', ['crop', 'reference', 'markup', 'mask'])
def test_selected_diagnosis_sees_clean_context_and_creates_no_edit(app_factory, kind):
    generator, analyzer = ScriptedGenerator(), ScriptedAnalyzer()
    with app_factory(generator=generator) as (client, app):
        _, slot = ready(client, app)
        app.state.hand_analyzer = analyzer
        before = len(generator.calls)
        result = analyze(client, slot, selected=True, kind=kind)
        assert result.status_code == 200, result.text
        body = result.json()
        assert body['scope'] == 'selection' and body['annotation_kind'] == kind
        assert len(body['annotation_sha256']) == 64 and body['image_size'] == list(SIZE)
        assert body['source_version_id'] == slot['current_version']['id']
        assert body['selected_region_index'] is None
        prompt = body['repair_prompts'][0]
        assert '中央两指末端粘连' in prompt and '恢复两指之间的间隙' in prompt
        assert '握花动作、戒指和花束遮挡' in prompt and '无法确认，不擅自补画' in prompt
        images = [part for part in analyzer.calls[0][0] if part['type'] == 'image_url']
        assert len(images) == 3
        decoded = [Image.open(io.BytesIO(base64.b64decode(part['image_url']['url'].split(',')[1]))) for part in images]
        assert decoded[0].size == SIZE and decoded[1].size == decoded[2].size == (1024, 1024)
        assert decoded[1].tobytes() != decoded[2].tobytes()
        assert len(generator.calls) == before
        assert len(client.get(f"/api/slots/{slot['id']}/versions").json()) == 1


def test_whole_image_detection_returns_separate_candidates(app_factory):
    with app_factory() as (client, app):
        _, slot = ready(client, app)
        analyzer = ScriptedAnalyzer(report(regions=[finding(), finding(label='画面右侧的手', bbox=[0.6, 0.3, 0.9, 0.6])]))
        app.state.hand_analyzer = analyzer
        result = analyze(client, slot)
        assert result.status_code == 200, result.text
        body = result.json()
        assert body['scope'] == 'detect' and len(body['regions']) == 2
        assert body['annotation_sha256'] is None and body['annotation_kind'] is None
        assert len([p for p in analyzer.calls[0][0] if p['type'] == 'image_url']) == 1
        assert '画面右侧的手' in body['repair_prompts'][1]


@pytest.mark.parametrize('status', ['normal', 'uncertain'])
def test_no_clear_defect_produces_no_repair_prompt(app_factory, status):
    with app_factory() as (client, app):
        _, slot = ready(client, app)
        app.state.hand_analyzer = ScriptedAnalyzer(report(status=status, summary='没有足够证据确认畸形。', regions=[]))
        body = analyze(client, slot, selected=True).json()
        assert body['status'] == status and body['repair_prompts'] == []


def test_uncertain_candidate_cannot_prescribe_a_repair():
    value = HandReport.model_validate(report(status='uncertain', regions=[finding(status='uncertain', goal='', evidence=[], problems=[])]))
    assert value.regions[0].repair_prompt() == ''
    with pytest.raises(ValueError):
        HandReport.model_validate(report(status='uncertain', regions=[finding(status='uncertain')]))


def test_reconstruction_is_an_explicit_proposal_instead_of_a_claimed_defect():
    value = HandReport.model_validate(report(status='reconstruct', regions=[finding(
        status='reconstruct', evidence=['袖口下方有数个弯曲指端，但接触处轮廓无法连续追踪'],
        problems=['交握接触处的指节和前后关系需要理顺'],
        goal='保留两处手腕位置，将前侧手指自然弯曲搭在后侧手掌外缘，理顺接触处各可见指节和指甲。',
    )]))
    prompt = value.regions[0].repair_prompt()
    assert '局部结构重建建议' in prompt and '不是已确认的畸形诊断' in prompt
    assert '前侧手指自然弯曲搭在后侧手掌外缘' in prompt
    assert '允许在标记范围内重新安排局部指节' in prompt
    assert '只修正上述有证据的问题' not in prompt


@pytest.mark.parametrize('missing', ['evidence', 'problems', 'goal'])
def test_reconstruction_still_requires_visible_basis_and_a_specific_goal(missing):
    value = finding(status='reconstruct')
    value[missing] = '' if missing == 'goal' else []
    with pytest.raises(ValueError):
        HandReport.model_validate(report(status='reconstruct', regions=[value]))


def test_reconstruction_status_is_consistent_with_the_available_plans():
    with pytest.raises(ValueError):
        HandReport.model_validate(report(status='uncertain', regions=[finding(status='reconstruct')]))
    with pytest.raises(ValueError):
        HandReport.model_validate(report(status='reconstruct', regions=[finding()]))
    mixed = HandReport.model_validate(report(regions=[finding(), finding(status='reconstruct')]))
    assert mixed.status == 'repair'


@pytest.mark.parametrize('status', ['repair', 'reconstruct'])
def test_longest_valid_plan_fits_the_existing_edit_prompt_limit(status):
    region = finding(status=status, label='目' * 80, goal='重' * 400,
                     evidence=['证' * 120] * 3, problems=['问' * 120] * 3,
                     preserve=['留' * 120] * 3, uncertainties=['疑' * 120] * 3)
    assert len(HandReport.model_validate(report(status=status, regions=[region])).regions[0].repair_prompt()) <= 2000


def test_user_goal_is_forwarded_as_a_hint_not_as_visual_evidence(app_factory):
    with app_factory() as (client, app):
        _, slot = ready(client, app)
        analyzer = ScriptedAnalyzer()
        app.state.hand_analyzer = analyzer
        result = client.post(f"/api/slots/{slot['id']}/hand-analysis", data={
            'source_version_id': slot['current_version']['id'], 'instruction': '希望理顺牵手处下缘的指尖',
        })
        assert result.status_code == 200, result.text
        hint = analyzer.calls[0][0][-1]['text']
        assert '希望理顺牵手处下缘的指尖' in hint and '不是已确认的图像事实' in hint


def test_selected_reconstruction_can_be_explicitly_used_and_saved(app_factory):
    with app_factory() as (client, app):
        _, slot = ready(client, app)
        app.state.hand_analyzer = ScriptedAnalyzer(report(status='reconstruct', regions=[finding(status='reconstruct')]))
        diagnosis = analyze(client, slot, selected=True).json()
        assert diagnosis['status'] == 'reconstruct' and diagnosis['selected_region_index'] is None
        assert diagnosis['repair_prompts'][0].startswith('局部结构重建建议')
        diagnosis['selected_region_index'] = 0
        result = client.post(f"/api/slots/{slot['id']}/regional-edit", data={
            'client_request_id': 'use-proposed-reconstruction', 'source_version_id': slot['current_version']['id'],
            'prompt': diagnosis['repair_prompts'][0], 'model': 'gpt-image-2.6-flare', 'provider': '88api', 'method': 'crop',
            'hand_analysis': json.dumps(diagnosis, ensure_ascii=False),
        }, files={'markup': ('mark.png', crop_annotation(), 'image/png')})
        assert result.status_code == 201, result.text
        wait_batch(client, result.json()['id'])
        with app.state.db.connect() as conn:
            task = conn.execute('SELECT * FROM generation_tasks WHERE batch_id=?', (result.json()['id'],)).fetchone()
        assert json.loads(task['model_config_snapshot'])['hand_analysis']['regions'][0]['status'] == 'reconstruct'
        assert task['prompt'] == diagnosis['repair_prompts'][0]


def test_analysis_validation_and_failures_never_start_an_edit(app_factory):
    generator, analyzer = ScriptedGenerator(), ScriptedAnalyzer(error=HandAnalysisError('hand_analysis_failed', '服务不可用'))
    with app_factory(generator=generator) as (client, app):
        _, slot = ready(client, app)
        app.state.hand_analyzer = analyzer
        before = len(generator.calls)
        invalid = analyze(client, slot, selected=True, content=b'invalid')
        assert invalid.status_code == 422 and not analyzer.calls
        stale = analyze(client, slot, version='other-version')
        assert stale.status_code == 409 and not analyzer.calls
        failed = analyze(client, slot)
        assert failed.status_code == 502 and failed.json()['detail']['code'] == 'hand_analysis_failed'
        assert len(analyzer.calls) == 1 and len(generator.calls) == before


def test_version_change_during_analysis_is_reported(app_factory):
    with app_factory() as (client, app):
        _, slot = ready(client, app)
        def change_version():
            with app.state.db.connect(write=True) as conn:
                conn.execute('UPDATE photo_slots SET current_version_id=NULL WHERE id=?', (slot['id'],))
        app.state.hand_analyzer = ScriptedAnalyzer(before_return=change_version)
        result = analyze(client, slot)
        assert result.status_code == 409 and result.json()['detail']['code'] == 'source_changed'


def test_diagnosis_is_bound_to_the_selection_and_saved_with_actual_prompt(app_factory):
    generator = ScriptedGenerator()
    with app_factory(generator=generator) as (client, app):
        _, slot = ready(client, app)
        app.state.hand_analyzer = ScriptedAnalyzer()
        body = analyze(client, slot, selected=True).json()
        body['selected_region_index'] = 0
        data = {'client_request_id': 'diagnosed-repair', 'source_version_id': slot['current_version']['id'],
                'prompt': body['repair_prompts'][0] + '\n保留原来的戒指高光。',
                'model': 'gpt-image-2.6-flare', 'provider': '88api', 'method': 'crop',
                'hand_analysis': json.dumps(body, ensure_ascii=False)}
        url = f"/api/slots/{slot['id']}/regional-edit"
        bad = client.post(url, data=data, files={'markup': ('mark.png', crop_annotation(box=(90, 180, 150, 240)), 'image/png')})
        assert bad.status_code == 422 and bad.json()['detail']['code'] == 'invalid_hand_analysis'
        result = client.post(url, data=data, files={'markup': ('mark.png', crop_annotation(), 'image/png')})
        assert result.status_code == 201, result.text
        wait_batch(client, result.json()['id'])
        duplicate = client.post(url, data=data, files={'markup': ('mark.png', crop_annotation(), 'image/png')})
        assert duplicate.status_code == 200 and duplicate.json()['id'] == result.json()['id']
        with app.state.db.connect() as conn:
            task = conn.execute('SELECT * FROM generation_tasks WHERE batch_id=?', (result.json()['id'],)).fetchone()
        snapshot = json.loads(task['model_config_snapshot'])
        assert snapshot['hand_analysis'] == body
        assert task['prompt'] == data['prompt'] and data['prompt'] in generator.calls[-1][1]
        assert task['source_version_id'] == body['source_version_id']


@pytest.mark.parametrize('mutation', ['source', 'selection', 'prompt', 'bbox'])
def test_invalid_diagnosis_metadata_is_rejected_before_generation(app_factory, mutation):
    with app_factory() as (client, app):
        _, slot = ready(client, app)
        app.state.hand_analyzer = ScriptedAnalyzer()
        body = analyze(client, slot).json()
        body['selected_region_index'] = 0
        if mutation == 'source': body['source_version_id'] = 'other-version'
        if mutation == 'selection': body['selected_region_index'] = None
        if mutation == 'prompt': body['repair_prompts'][0] = 'invented prompt'
        if mutation == 'bbox': body['regions'][0]['bbox'][0] = -0.1
        result = client.post(f"/api/slots/{slot['id']}/regional-edit", data={
            'client_request_id': 'invalid', 'source_version_id': slot['current_version']['id'],
            'prompt': '修复手指', 'model': 'gpt-image-2.6-flare', 'provider': '88api', 'method': 'crop',
            'hand_analysis': json.dumps(body),
        }, files={'markup': ('mark.png', crop_annotation(), 'image/png')})
        assert result.status_code == 422 and result.json()['detail']['code'] == 'invalid_hand_analysis'


def test_missing_vision_model_is_rejected_before_calling_provider(app_factory):
    with app_factory() as (client, app):
        _, slot = ready(client, app)
        app.state.config = replace(app.state.config, generator='88api', api_key='test-key')
        app.state.db.set_director_model(None)
        analyzer = ScriptedAnalyzer()
        app.state.hand_analyzer = analyzer
        result = analyze(client, slot)
        assert result.status_code == 409 and result.json()['detail']['code'] == 'hand_analysis_unavailable'
        assert not analyzer.calls


def test_analyzer_uses_the_selected_director_service_and_accepts_json_fences(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url.host == 'vision.example' and request.headers['authorization'] == 'Bearer vision-only-key'
        assert body['model'] == 'vision-model' and body['temperature'] == 0.2
        assert '不能因为没有看到五根手指就判断缺指' in body['messages'][0]['content']
        return httpx.Response(200, json={'choices': [{'message': {'content': '```json\n' + json.dumps(report()) + '\n```'}}]})
    config = Config(tmp_path, 1, 'mock', 0, director_pptoken_api_key='vision-only-key', pptoken_base_url='https://vision.example')
    analyzer = ApiHandAnalyzer(config, transport=httpx.MockTransport(handler))
    result = analyzer.analyze([{'type': 'text', 'text': 'diagnose'}], model='vision-model', provider='pptoken')
    assert result.status == 'repair' and len(calls) == 1


@pytest.mark.parametrize('bad', ['out-of-bounds', 'reversed', 'nan', 'missing-evidence', 'status', 'not-json', 'extra-field', 'multiple-selected'])
def test_invalid_provider_output_is_explicit_and_is_not_retried(tmp_path, bad):
    value = copy.deepcopy(report())
    if bad == 'out-of-bounds': value['regions'][0]['bbox'][2] = 1.2
    if bad == 'reversed': value['regions'][0]['bbox'] = [0.8, 0.2, 0.3, 0.4]
    if bad == 'nan': value['regions'][0]['bbox'][0] = float('nan')
    if bad == 'missing-evidence': value['regions'][0]['evidence'] = []
    if bad == 'status': value['status'] = 'normal'
    if bad == 'extra-field': value['extra'] = 'unvalidated'
    if bad == 'multiple-selected': value['regions'].append(finding())
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'not JSON' if bad == 'not-json' else json.dumps(value)}}]})
    analyzer = ApiHandAnalyzer(Config(tmp_path, 1, 'mock', 0, director_api_key='test-key'), transport=httpx.MockTransport(handler))
    with pytest.raises(HandAnalysisError) as exc:
        analyzer.analyze([{'type': 'text', 'text': 'selected'}], model='vision', provider='88api', selection=True)
    assert exc.value.code == 'hand_analysis_invalid_output' and len(calls) == 1


def test_vision_inputs_use_oriented_dimensions_and_remove_metadata(tmp_path):
    source = tmp_path / 'portrait.jpg'
    image = Image.new('RGB', (400, 200), 'white')
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = 'private metadata'
    image.save(source, exif=exif)
    parts, size = analysis_inputs(source, None, 'crop')
    assert size == (200, 400)
    data = base64.b64decode(parts[1]['image_url']['url'].split(',')[1])
    with Image.open(io.BytesIO(data)) as outgoing:
        assert outgoing.size == (200, 400) and not outgoing.getexif()


def test_selection_cannot_be_silently_cut_off_by_the_square_crop(tmp_path):
    source = tmp_path / 'portrait.png'
    Image.new('RGB', (256, 768), 'white').save(source)
    with pytest.raises(ValueError, match='跨度过大'):
        analysis_inputs(source, crop_annotation(size=(256, 768), box=(30, 30, 210, 700)), 'crop')
