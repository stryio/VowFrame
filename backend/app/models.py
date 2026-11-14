"""Built-in image model registry and validation helpers."""

from dataclasses import dataclass

# Image services (V7.11): 88API, or PPTOKEN when its key is configured. Settings pick the one models that both
# offer go to; a model only one of them offers always goes there.
# 2026-11-13: 火山方舟 (Volcengine Ark), the official Seedream API; it only makes images, so the AI director cannot
# use it (DIRECTOR_PROVIDERS).
PROVIDERS = {'88api': '88API', 'pptoken': 'PPTOKEN', 'ark': '火山方舟'}
DIRECTOR_PROVIDERS = ('88api', 'pptoken')
DEFAULT_PROVIDER = '88api'


@dataclass(frozen=True)
class ModelDefinition:
    id: str
    name: str
    family: str
    max_reference_images: int
    description: str
    providers: tuple[str, ...] = ('88api',)


MODEL_REGISTRY = (
    ModelDefinition('gpt-image-2', 'GPT Image 2', 'openai', 4, 'OpenAI，按张计费，参考图≤4', ('88api', 'pptoken')),
    ModelDefinition('gpt-image-2.5', 'GPT Image 2.5', 'openai', 10, 'OpenAI，PPTOKEN，参考图≤10', ('pptoken',)),
    # 2026-11-05: PPTOKEN lists Flare and Sunburst too (its /v1/models), so they go to the selected service.
    ModelDefinition('gpt-image-2.5-flare', 'GPT Image 2.5 Flare', 'openai', 10, 'OpenAI，按张计费，参考图≤10', ('88api', 'pptoken')),
    ModelDefinition('gpt-image-2.5-sunburst', 'GPT Image 2.5 Sunburst', 'openai', 10, 'OpenAI，按张计费，参考图≤10', ('88api', 'pptoken')),
    # 2026-11-05, user: 先加进去…88api也加; PPTOKEN opened 2.6, 2.6 Sunburst and 2.6 Flare later that day.
    ModelDefinition('gpt-image-2.6', 'GPT Image 2.6', 'openai', 10, 'OpenAI，参考图≤10', ('88api', 'pptoken')),
    ModelDefinition('gpt-image-2.6-sunburst', 'GPT Image 2.6 Sunburst', 'openai', 10, 'OpenAI，参考图≤10', ('88api', 'pptoken')),
    ModelDefinition('gpt-image-2.6-flare', 'GPT Image 2.6 Flare', 'openai', 10, 'OpenAI，参考图≤10', ('88api', 'pptoken')),
    ModelDefinition('gemini-nano-banana-2.1', 'Gemini Nano Banana 2.1', 'gemini', 6, 'Gemini，按次计费，参考图≤6'),
    # V7.28: Nano Banana Pro, the user's model for washes; same chat/completions call and 1K/2K/4K image_size as 2.1.
    ModelDefinition('gemini-3-pro-image', 'Nano Banana Pro', 'gemini', 14, 'Gemini 3 Pro Image，动态计费，参考图≤14'),
    # 2026-11-13, user: 新增一个Doubao-Seedream-5.5，官方接口. Ark's own JSON endpoint (app/ark.py); at most 2K, so
    # it cannot wash (4K).
    ModelDefinition('doubao-seedream-5-5-261028', 'Seedream 5.5', 'seedream', 10, '火山方舟官方，最高 2K，参考图≤10',
                    ('ark',)),
)
MODELS_BY_ID = {model.id: model for model in MODEL_REGISTRY}
DEFAULT_MODEL = 'gpt-image-2'


def provider_for(model: ModelDefinition, selected: str) -> str:
    """The service a request for this model goes to: the selected one when it offers the model."""
    return selected if selected in model.providers else model.providers[0]


def provider_models(provider: str) -> list[ModelDefinition]:
    return [model for model in MODEL_REGISTRY if provider in model.providers]


def get_model(model_id: str) -> ModelDefinition | None:
    return MODELS_BY_ID.get(model_id)


def require_model(model_id: str) -> ModelDefinition:
    model = get_model(model_id)
    if model is None:
        raise ValueError(f'不支持模型：{model_id}')
    return model
