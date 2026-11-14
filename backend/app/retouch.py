"""Independent retouch controls and the presets used by older projects and candidates."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


AdjustmentLevel = Literal['off', 'minimal', 'subtle', 'light', 'mild', 'balanced', 'moderate', 'enhanced', 'strong', 'intense', 'maximum']
ConcealingLevel = Literal['off', 'minimal', 'subtle', 'light', 'mild', 'standard', 'moderate', 'enhanced', 'strong', 'intense', 'full']


class RetouchSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    slimming: AdjustmentLevel
    brightening: AdjustmentLevel
    concealing: ConcealingLevel
    chin: AdjustmentLevel
    nasolabial: AdjustmentLevel


def retouch_preset(level: str) -> RetouchSettings:
    """Match the existing levels; each role interprets brightening for its own skin."""
    return {
        'light': RetouchSettings(slimming='off', brightening='light', concealing='light', chin='off', nasolabial='off'),
        'natural': RetouchSettings(slimming='off', brightening='moderate', concealing='standard', chin='light', nasolabial='light'),
        'refined': RetouchSettings(slimming='light', brightening='moderate', concealing='full', chin='light', nasolabial='light'),
    }[level]


def stored_retouch_settings(value: str | None, level: str) -> RetouchSettings:
    """NULL explicitly means a legacy preset; invalid stored settings must not be silently ignored."""
    return retouch_preset(level) if value is None else RetouchSettings.model_validate_json(value)


def candidate_retouch_settings(candidate) -> RetouchSettings:
    return stored_retouch_settings(candidate['retouch_settings'], candidate['makeup_level'])


def project_retouch_settings(project, role: str) -> RetouchSettings:
    level = project['makeup_level'] if role == 'bride' else project['groom_makeup_level']
    return stored_retouch_settings(project[f'{role}_retouch_settings'], level)


def has_face_shaping(candidate) -> bool:
    return candidate_retouch_settings(candidate).slimming != 'off'
