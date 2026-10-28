"""Built-in image model registry and validation helpers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelDefinition:
    id: str
    name: str
    family: str
    max_reference_images: int
    description: str


MODEL_REGISTRY = (
    ModelDefinition('gpt-image-2', 'GPT Image 2', 'openai', 4, 'OpenAI，按张计费，参考图≤4'),
    ModelDefinition('gpt-image-2.5-flare', 'GPT Image 2.5 Flare', 'openai', 10, 'OpenAI，按张计费，参考图≤10'),
    ModelDefinition('gpt-image-2.5-sunburst', 'GPT Image 2.5 Sunburst', 'openai', 10, 'OpenAI，按张计费，参考图≤10'),
    ModelDefinition('gemini-nano-banana-2.1', 'Gemini Nano Banana 2.1', 'gemini', 6, 'Gemini，按次计费，参考图≤6'),
)
MODELS_BY_ID = {model.id: model for model in MODEL_REGISTRY}
DEFAULT_MODEL = 'gpt-image-2'


def get_model(model_id: str) -> ModelDefinition | None:
    return MODELS_BY_ID.get(model_id)


def require_model(model_id: str) -> ModelDefinition:
    model = get_model(model_id)
    if model is None:
        raise ValueError(f'不支持模型：{model_id}')
    return model
