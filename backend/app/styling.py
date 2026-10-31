"""Styled photos (V4.0): which styled photo stands in for an uploaded photo, and the styling record on tasks.

A shot of a style no longer asks the image model to restyle hair, skin and makeup: the edit endpoint copies what
the reference photos show. Each person's photo is edited once per style into a styled photo (TaskManager does the
edit right before the first shot that needs it), and every shot and face swap of that style uses the styled photo.

A shot task records the styling it was created with in its model snapshot:
    'styling': {'version': STYLING_VERSION, 'bride': {'makeup': level, 'hair': length}, 'groom': {...}}
    'reference_slots': one [role, angle] per reference image, null for a sample reference
The task's reference images start as the uploaded photos and are replaced by the styled photos when it runs, so a
replay ('same') reuses exactly the styled photos the original used.
"""

from .face_swap import face_swap_reference_ids
from .prompts import STYLING_VERSION

# Order of face_swap_reference_ids().
FACE_SWAP_SLOTS = (('bride', 'front'), ('groom', 'front'), ('bride', 'left'), ('bride', 'right'),
                   ('groom', 'left'), ('groom', 'right'))


def styling_snapshot(project) -> dict:
    return {
        'version': STYLING_VERSION,
        'bride': {'makeup': project['makeup_level'], 'hair': project['bride_hair_length']},
        'groom': {'makeup': project['groom_makeup_level'], 'hair': project['groom_hair_length']},
    }


def reference_slots(side_references: list[tuple[str, str]], with_preview: bool) -> list:
    """[role, angle] for each image of app.api._build_references, in its fixed order."""
    slots = [['bride', 'front'], ['groom', 'front']]
    slots.extend([role, facing] for role, facing in side_references)
    if with_preview:
        slots.append(None)
    return slots


def find_styled_photo(conn, project_id: str, style_id: str, role: str, angle: str, source_asset_id: str,
                      styling: dict) -> str | None:
    """Image asset of the current styled photo for this photo, style and styling; None when there is none yet."""
    person = styling[role]
    row = conn.execute(
        'SELECT image_asset_id FROM styled_photos WHERE project_id=? AND style_id=? AND role=? AND angle=? '
        'AND source_asset_id=? AND makeup_level=? AND hair_length=? AND styling_version=? AND discarded_at IS NULL '
        'ORDER BY created_at DESC,rowid DESC LIMIT 1',
        (project_id, style_id, role, angle, source_asset_id, person['makeup'], person['hair'], styling['version']),
    ).fetchone()
    return row['image_asset_id'] if row else None


def styled_face_swap_reference_ids(conn, project, style_id: str, styling: dict | None) -> list[str | None]:
    """Face-swap sources for a picture shot with this styling: each photo's styled version where one exists.

    The swap must use the styled faces, or it would wash the makeup and skin retouching back out. A side photo the
    shots never needed has no styled version; it keeps the uploaded photo (FaceFusion only borrows the identity of
    a turned face from it). Pictures from before V4.0 (no styling) keep the uploaded photos.
    """
    references = face_swap_reference_ids(project)
    if not styling:
        return references
    return [None if asset_id is None else
            find_styled_photo(conn, project['id'], style_id, role, angle, asset_id, styling) or asset_id
            for asset_id, (role, angle) in zip(references, FACE_SWAP_SLOTS)]


def uploaded_asset_id(conn, asset_id: str) -> str:
    """The uploaded photo behind a styled photo; any other asset is returned as it is."""
    row = conn.execute('SELECT asset_type,source_asset_id FROM image_assets WHERE id=?', (asset_id,)).fetchone()
    if row is not None and row['asset_type'] == 'styled' and row['source_asset_id']:
        return row['source_asset_id']
    return asset_id
