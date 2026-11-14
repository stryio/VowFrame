"""Which way a face turns in a shot, and which uploaded photo shows it (V7.48).

Each person uploads a front photo and, optionally, up to two side photos per direction: a half side (about 45
degrees, both eyes visible) and a full side (about 90 degrees, a profile). The director may only turn a clearly
visible face to an angle the person has a photo of, because the image model draws an angle it has never seen from
guesses, and the likeness drops (2026-11-05: a bride's side face scored 0.60 with no side reference, 0.73-0.79 with).

'left' and 'right' keep their meaning from before V7.48 (the one side photo per direction, now the half side), so
older drafts, tasks and side looks read the same.
"""

FRONT = 'front'
SIDE_FACINGS = ('left', 'right', 'left_full', 'right_full')
FACINGS = (FRONT, *SIDE_FACINGS)
DIRECTION_WORDS = {'left': '左', 'right': '右'}


def direction(facing: str) -> str:
    """'front', 'left' or 'right': the side of the picture the face turns to."""
    return facing.removesuffix('_full')


def is_full(facing: str) -> bool:
    return facing.endswith('_full')


def upload_column(person: str, facing: str) -> str:
    """The projects column holding this person's photo of this facing."""
    return f'person_{person}_image_id' if facing == FRONT else f'person_{person}_{facing}_image_id'


def facing_phrase(facing: str) -> str:
    """How an image label names a side angle: 「脸朝画面左侧」, with 「（正侧面）」 for a full side."""
    return f'脸朝画面{DIRECTION_WORDS[direction(facing)]}侧' + ('（正侧面）' if is_full(facing) else '')


def uploaded_side(project, person: str, facing: str) -> tuple[str | None, str | None]:
    """(asset_id, facing of that photo) for a side facing: its own photo, else the other photo of the same direction
    (a shot the director may turn freely, e.g. a back view, still gets the nearest angle), else (None, None)."""
    other = direction(facing) if is_full(facing) else facing + '_full'
    for candidate in (facing, other):
        asset_id = project[upload_column(person, candidate)]
        if asset_id:
            return asset_id, candidate
    return None, None


def available_facings(project, person: str) -> tuple[str, ...]:
    """The facings this person has a photo of: front always, then each uploaded side photo."""
    return (FRONT, *(facing for facing in SIDE_FACINGS if project[upload_column(person, facing)]))
