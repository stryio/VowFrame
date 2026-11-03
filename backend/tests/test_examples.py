"""Catalogue generation follows app settings and never changes paid services after a failure."""

from dataclasses import replace

import pytest

from app.config import Config
from app.db import Database
from app.generator import GeneratorError, OutcomeKind
from scripts import make_examples


def test_examples_follow_the_selected_provider_and_model_without_changing_settings(tmp_path):
    config = Config(tmp_path / 'data', 1, 'mock', 0, api_key='first-key', pptoken_api_key='second-key',
                    model='gpt-image-2.5-sunburst')
    db = Database(config.db_path)
    db.initialize()
    db.set_default_model('gpt-image-2.5')
    db.set_image_provider('pptoken')

    selected, provider = make_examples.example_config(config)
    assert provider == 'PPTOKEN' and selected.model == 'gpt-image-2.5'
    assert selected.api_key == config.pptoken_api_key and selected.base_url == config.pptoken_base_url
    assert selected.resolution == '1K'
    assert db.get_default_model() == 'gpt-image-2.5' and db.get_image_provider() == 'pptoken'
    with pytest.raises(ValueError, match='PPTOKEN 没有配置密钥'):
        make_examples.example_config(replace(config, pptoken_api_key=''))


def test_examples_before_the_first_app_run_use_local_configuration(tmp_path):
    config = Config(tmp_path / 'data', 1, 'mock', 0, api_key='first-key', model='gpt-image-2.5-sunburst')
    selected, provider = make_examples.example_config(config)
    assert provider == '88API' and selected.model == config.model and selected.api_key == config.api_key
    assert not config.db_path.exists()


def test_quota_failure_stops_jobs_before_they_can_send_requests(monkeypatch):
    monkeypatch.setattr(make_examples, 'WORKERS', 1)
    sent = []

    def rejected():
        sent.append('first')
        raise GeneratorError(OutcomeKind.AUTH_OR_QUOTA)

    failures = make_examples.run({'first': rejected, 'second': lambda: sent.append('second'),
                                  'third': lambda: sent.append('third')})
    assert sent == ['first'] and set(failures) == {'first', 'second', 'third'}
