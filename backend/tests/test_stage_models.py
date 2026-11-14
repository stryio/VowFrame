"""2026-11-13, user: 每个步骤/阶段的修图模型都可以自定义选择. The makeup, the look (side looks too), the style art, the
AI face fix and the regional edit each have their own service and model in Settings, like the hand fix and the wash."""

from conftest import create_project, ensure_looks, wait_until

NANO, GPT26, PRO = 'gemini-nano-banana-2.1', 'gpt-image-2.6', 'gemini-3-pro-image'


def test_makeups_looks_and_style_art_use_their_own_model_and_service(app_factory):
    with app_factory(style_art='on') as (client, app):
        settings = client.patch('/api/settings', json={
            'makeup_model': NANO, 'look_provider': 'pptoken', 'look_model': GPT26, 'style_art_model': PRO}).json()
        assert (settings['makeup_model'], settings['look_provider'], settings['look_model'], settings['style_art_model']) == (
            NANO, 'pptoken', GPT26, PRO)
        project = create_project(client)
        ensure_looks(client, project['id'], ['city_story'])
        stylist = app.state.stylist
        # Makeups on the image service (no service named: it follows), looks on PPTOKEN.
        assert set(zip(stylist.models, stylist.providers)) == {(NANO, None), (GPT26, 'pptoken')}
        made = len(stylist.models)
        from app.rooms import default_room
        response = client.post('/api/style-arts/city_story/generate', params={'room': default_room('city_story')})
        assert response.status_code == 202, response.text
        wait_until(lambda: next(item for item in client.get('/api/style-arts').json() if item['style_id'] == 'city_story'),
                   lambda item: item['status'] in ('ready', 'failed'))
        assert set(stylist.models[made:]) == {PRO}


def test_a_step_falls_back_to_the_default_model_and_follows_a_service_change(app_factory):
    with app_factory() as (client, app):
        db = app.state.db
        assert db.get_edit_choice('makeup') == ('88api', 'gpt-image-2')   # nothing chosen: the default model
        client.patch('/api/settings', json={'makeup_model': NANO})
        # PPTOKEN has no Nano Banana: moving the makeup there drops the model, which then follows the default.
        settings = client.patch('/api/settings', json={'makeup_provider': 'pptoken'}).json()
        assert (settings['makeup_provider'], settings['makeup_model']) == ('pptoken', None)
        assert client.patch('/api/settings', json={'look_provider': 'pptoken', 'look_model': PRO}).status_code == 422


def test_the_regional_edit_model_must_be_one_that_can_edit_regions(app_factory):
    with app_factory() as (client, app):
        assert client.patch('/api/settings', json={'regional_model': 'gpt-image-2'}).status_code == 422
        assert client.patch('/api/settings', json={'regional_model': PRO}).status_code == 422
        settings = client.patch('/api/settings', json={'regional_model': NANO}).json()
        assert settings['regional_model'] == NANO
