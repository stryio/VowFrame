"""Reference themes use the existing look, outfit, sample and director pipeline."""

import json

import pytest

from app.director import _shot_requirement
from app.generator import GeneratorError, OutcomeKind
from app.prompts import (OUTFIT_BY_ID, STYLE_BY_ID, STYLE_OUTFITS, compose_preview_prompt,
                         director_scene_text, outfit_text)
from app.reference_styles import REFERENCE_STYLE_IDS, REFERENCE_STYLES
from conftest import PNG, create_director_photo_batch, create_project, preview_batch, wait_batch
from scripts.review_reference_styles import load_plan, plan_context, render_picture
from test_styling import batch_tasks, detail


@pytest.mark.parametrize('style_id', REFERENCE_STYLE_IDS)
def test_each_reference_theme_can_make_looks_sample_and_set(client_app, style_id):
    client, app = client_app
    styles = {style['id']: style for style in client.get('/api/styles').json()}
    assert style_id in styles and not styles[style_id]['retired']
    options = client.get('/api/look-options').json()
    outfit = STYLE_OUTFITS[style_id][0]
    assert options['style_outfits'][style_id] == [outfit]
    assert any(item['id'] == outfit for item in options['outfits'])
    project = create_project(client)
    preview = preview_batch(client, project['id'], style_ids=[style_id])
    wait_batch(client, preview['id'])
    task = batch_tasks(client, preview['id'])[0]
    assert outfit_text(outfit) in task['prompt'] and STYLE_BY_ID[style_id]['look'] in task['prompt']
    assert STYLE_BY_ID[style_id]['preview'] in task['prompt']
    sample = detail(client, project['id'])['previews'][0]

    class RecordingDirector:
        def write_moments(self, *args, **kwargs):
            self.requirement = args[3]
            self.scene = args[1]
            return previous.write_moments(*args, **kwargs)

    previous = app.state.director
    recorder = RecordingDirector()
    app.state.director = recorder
    photos = create_director_photo_batch(client, project['id'], sample['id'], request_id='reference-set')
    assert photos.status_code == 201, photos.text
    wait_batch(client, photos.json()['id'])
    tasks = batch_tasks(client, photos.json()['id'])
    assert len(tasks) == 4 and all(outfit_text(outfit) in item['prompt'] for item in tasks)
    assert STYLE_BY_ID[style_id]['shooting'] in recorder.scene
    if STYLE_BY_ID[style_id]['atmospheric']:
        assert '氛围镜头 1 张' in recorder.requirement
        assert '互动 2 张' in recorder.requirement
    else:
        assert '这组不安排氛围镜头' in recorder.requirement


def test_nine_themes_are_distinct_and_keep_the_original_catalogue():
    assert len(REFERENCE_STYLE_IDS) == len(set(REFERENCE_STYLE_IDS)) == 9
    for old in ('korean_minimal', 'forest_natural', 'french_vintage', 'luxury_interior', 'chinese_traditional'):
        assert old in STYLE_BY_ID and old not in REFERENCE_STYLE_IDS
    assert '香槟金' in OUTFIT_BY_ID['champagne_couture'].bride
    assert '宽檐' in OUTFIT_BY_ID['french_flower_hat'].headwear
    assert '短网纱' in OUTFIT_BY_ID['celebration_qipao'].headwear
    for style in REFERENCE_STYLES:
        sid = style['id']
        assert style['look'] in compose_preview_prompt(sid, '3:4', styled=True, outfit=style['outfits'][0])
        for single in (True, False):
            assert style['shooting'] in director_scene_text(sid, single_variant=single, outfit=style['outfits'][0])


def test_short_set_atmosphere_changes_only_opted_in_themes():
    assert '这组不安排氛围镜头' in _shot_requirement(4)
    assert '氛围镜头 1 张' in _shot_requirement(4, atmospheric=True)
    for count in (1, 8, 12, 20):
        assert _shot_requirement(count, atmospheric=True) == _shot_requirement(count)


def test_review_plan_rejects_stale_specs_before_sending_image_requests(tmp_path):
    context = plan_context('blue_hour_veil', 4, False)
    path = tmp_path / 'plan.json'
    changed = {**context, 'outfit': 'studio_satin'}
    path.write_text(json.dumps({'context': changed, 'moments': []}))
    with pytest.raises(ValueError, match='规格已经变化'):
        load_plan(path, context)


def test_quota_rejection_is_recorded_and_an_explicit_later_run_can_resume(tmp_path):
    path = tmp_path / 'shot.jpg'

    class QuotaGenerator:
        def generate_image(self, *args):
            raise GeneratorError(OutcomeKind.AUTH_OR_QUOTA)

    class FundedGenerator:
        def generate_image(self, *args):
            return PNG

    with pytest.raises(GeneratorError):
        render_picture(QuotaGenerator(), [], '镜头。', 'test-model', '3:4', path, provider='test', resolution='1K')
    record = json.loads(path.with_suffix('.attempt.json').read_text())
    assert record['status'] == 'failed' and record['kind'] == 'auth_or_quota'
    assert not path.exists()
    render_picture(FundedGenerator(), [], '镜头。', 'test-model', '3:4', path, provider='test', resolution='1K')
    record = json.loads(path.with_suffix('.attempt.json').read_text())
    assert path.exists() and record['status'] == 'completed'
    assert record['previous_outcomes'][0]['kind'] == 'auth_or_quota'


def test_unknown_paid_outcome_is_not_sent_again_on_a_later_run(tmp_path):
    path = tmp_path / 'shot.jpg'
    path.with_suffix('.attempt.json').write_text(json.dumps({'status': 'requested'}))

    class MustNotCall:
        def generate_image(self, *args):
            pytest.fail('an ambiguous paid request must not be repeated')

    with pytest.raises(ValueError, match='结果不明'):
        render_picture(MustNotCall(), [], '镜头。', 'test-model', '3:4', path, provider='test', resolution='1K')
