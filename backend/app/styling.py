"""Looks (V5.0): styled photos the user generates and picks, and which look a shot uses.

The image edit endpoint copies what the reference photos show, so hairstyle, skin and makeup cannot be asked of
a shot. Instead each person's uploaded photo is edited into "looks" when the user asks for candidates (one edit
each: a hairstyle and, for the bride, a makeup colour family); the user selects some, and every sample is shot
from one selected look per person. Photo sets and slot actions keep the looks of the picture they start from.

A shot task records its looks in its model snapshot:
    'looks': {'version': STYLING_VERSION, 'bride': look id, 'groom': look id}
    'reference_slots': one [role, angle] per reference image, null for a sample reference
The task's reference images hold the front looks from the start; a side photo the shot needs is replaced by its
side look (derived from the side photo and the front look) when the task runs, so a replay ('same') reuses
exactly the images the original used.

V4.0 tasks recorded 'styling' instead and styled each photo per style when they ran; they still replay, but that
automatic styling is retired.
"""

from .face_swap import face_swap_reference_ids
from .prompts import HAIRSTYLE_BY_KEY, MAKEUP_COLOR_BY_ID, STYLE_BY_ID, STYLE_MAKEUP_COLORS, STYLES, STYLING_VERSION, UPDO_STYLES

# Order of face_swap_reference_ids().
FACE_SWAP_SLOTS = (('bride', 'front'), ('groom', 'front'), ('bride', 'left'), ('bride', 'right'),
                   ('groom', 'left'), ('groom', 'right'))
MAX_SELECTED_LOOKS = 3
# Candidates offered first: the photo's own hairstyle with the most neutral colours, an updo in classic red
# for the Chinese styles, and soft waves in peach coral for outdoor styles. Unavailable ones for the stated hair
# length fall back to the photo's own hairstyle, keeping the colour.
DEFAULT_LOOKS = {
    'bride': (('keep', 'milk_tea'), ('low_bun', 'classic_red'), ('soft_waves', 'peach_coral')),
    'groom': (('keep', None), ('side_part', None), ('textured', None)),
}


def person_of(project, role: str) -> str:
    return 'a' if project['person_a_role'] == role else 'b'


def role_of(project, person: str) -> str:
    if person == 'a':
        return project['person_a_role']
    return 'groom' if project['person_a_role'] == 'bride' else 'bride'


def hair_length_of(project, role: str) -> str:
    return project['bride_hair_length'] if role == 'bride' else project['groom_hair_length']


def makeup_level_of(project, role: str) -> str:
    return project['makeup_level'] if role == 'bride' else project['groom_makeup_level']


def default_look_items(project, role: str) -> list[tuple[str, str | None]]:
    """The default candidates for this person's stated hair length, without duplicates."""
    length = hair_length_of(project, role)
    items = []
    for hairstyle_id, color in DEFAULT_LOOKS[role]:
        if length not in HAIRSTYLE_BY_KEY[(role, hairstyle_id)].lengths:
            hairstyle_id = 'keep'
        if (hairstyle_id, color) not in items:
            items.append((hairstyle_id, color))
    return items


def look_problem(project, look) -> str | None:
    """Why a front look cannot be used for a new shot; None when it can."""
    if look['angle'] != 'front' or look['discarded_at'] is not None:
        return 'unavailable'
    if look['status'] != 'ready' or look['image_asset_id'] is None:
        return 'not_ready'
    if role_of(project, look['person']) != look['role']:
        return 'role_changed'
    if project[f"person_{look['person']}_image_id"] != look['source_asset_id']:
        return 'photo_replaced'
    return None


LOOK_PROBLEM_MESSAGES = {
    'unavailable': '这套造型不存在或已删除',
    'not_ready': '这套造型还没有生成好',
    'role_changed': '交换新娘和新郎之后，这套造型不能再用于新照片',
    'photo_replaced': '这个人的照片已经更换，这套造型不能再用于新照片',
}


def fits_style(look, style_id: str) -> bool:
    """The Chinese styles need the bride in an updo; a bride with short hair keeps her own hairstyle."""
    if style_id not in UPDO_STYLES or look['role'] != 'bride':
        return True
    return HAIRSTYLE_BY_KEY[('bride', look['hairstyle_id'])].updo or (look['hair_length'] == 'short' and look['hairstyle_id'] == 'keep')


def selected_looks(conn, project, role: str) -> list:
    """This person's selected looks that can still be used, in their order (the first is the default)."""
    rows = conn.execute(
        "SELECT * FROM looks WHERE project_id=? AND person=? AND angle='front' AND selected_order IS NOT NULL "
        'AND discarded_at IS NULL ORDER BY selected_order,rowid', (project['id'], person_of(project, role))).fetchall()
    return [row for row in rows if look_problem(project, row) is None]


def color_match(style_id: str, color: str | None) -> str | None:
    """'primary' / 'alternate' / 'other' for the bride's colour against the style's recommendation."""
    if color is None:
        return None
    primary, alternates = STYLE_MAKEUP_COLORS[style_id]
    return 'primary' if color == primary else 'alternate' if color in alternates else 'other'


def default_assignment(conn, project) -> dict:
    """Per style, the looks a new sample uses unless the user picks others.

    Among the selected looks that fit the style: the first in the style's recommended colour, else the first in
    one of its alternate colours, else the first. Shown before generating, so the rule never acts unseen.
    """
    brides, grooms = selected_looks(conn, project, 'bride'), selected_looks(conn, project, 'groom')
    result = {}
    for style in STYLES:
        fitting = [look for look in brides if fits_style(look, style['id'])]
        bride = None
        for wanted in ('primary', 'alternate', 'other'):
            bride = next((look for look in fitting if color_match(style['id'], look['makeup_color']) == wanted), None)
            if bride is not None:
                break
        result[style['id']] = {'bride': bride['id'] if bride else None, 'groom': grooms[0]['id'] if grooms else None}
    return result


def look_snapshot(bride_look_id: str, groom_look_id: str) -> dict:
    return {'version': STYLING_VERSION, 'bride': bride_look_id, 'groom': groom_look_id}


def reference_slots(side_references: list[tuple[str, str]], with_preview: bool) -> list:
    """[role, angle] for each image of app.api._build_references, in its fixed order."""
    slots = [['bride', 'front'], ['groom', 'front']]
    slots.extend([role, facing] for role, facing in side_references)
    if with_preview:
        slots.append(None)
    return slots


def find_side_look(conn, parent_look_id: str, angle: str, source_asset_id: str) -> str | None:
    """Image asset of the side look made from this side photo and front look; None when there is none yet."""
    row = conn.execute(
        "SELECT image_asset_id FROM looks WHERE parent_look_id=? AND angle=? AND source_asset_id=? AND status='ready' "
        'AND discarded_at IS NULL ORDER BY created_at DESC,rowid DESC LIMIT 1',
        (parent_look_id, angle, source_asset_id),
    ).fetchone()
    return row['image_asset_id'] if row else None


def task_face_swap_reference_ids(conn, project, snapshot: dict, references: list) -> list[str | None]:
    """Face-swap sources for a picture: the faces the shot itself was made from.

    The swap must use the styled faces, or it would wash the makeup and skin retouching back out, and it must use
    the picture's own ones, not whatever the project has now. Fronts are the task's first two references; a side
    photo the shot used is its reference too; another side photo uses its side look when one exists, else the
    uploaded side photo (FaceFusion only borrows the identity of a turned face from it). Pictures from before
    styled photos keep the project's uploaded photos.
    """
    uploads = face_swap_reference_ids(project)
    looks, styling = snapshot.get('looks'), snapshot.get('styling')
    if not looks and not styling:
        return uploads
    used = {}
    for asset_id, slot in zip(references, snapshot.get('reference_slots') or []):
        if slot is not None:
            used[tuple(slot)] = asset_id
    result = []
    for upload, slot in zip(uploads, FACE_SWAP_SLOTS):
        if slot in used:
            result.append(used[slot])
        elif upload is None:
            result.append(None)
        elif looks:
            result.append(find_side_look(conn, looks[slot[0]], slot[1], upload) or upload)
        else:
            result.append(upload)
    return result


def uploaded_asset_id(conn, asset_id: str) -> str:
    """The uploaded photo behind a styled photo or look; any other asset is returned as it is."""
    row = conn.execute('SELECT asset_type,source_asset_id FROM image_assets WHERE id=?', (asset_id,)).fetchone()
    if row is not None and row['asset_type'] == 'styled' and row['source_asset_id']:
        return row['source_asset_id']
    return asset_id


def look_response(project, look) -> dict:
    hairstyle = HAIRSTYLE_BY_KEY.get((look['role'], look['hairstyle_id']))
    color = MAKEUP_COLOR_BY_ID.get(look['makeup_color']) if look['makeup_color'] else None
    problem = look_problem(project, look)
    return {
        'id': look['id'], 'person': look['person'], 'role': look['role'],
        'hairstyle_id': look['hairstyle_id'], 'hairstyle_name': hairstyle.name if hairstyle else look['hairstyle_id'],
        'makeup_color': look['makeup_color'], 'makeup_color_name': color.name if color else None,
        'makeup_level': look['makeup_level'], 'hair_length': look['hair_length'],
        'status': look['status'], 'error_code': look['error_code'], 'error_message': look['error_message'],
        'asset_id': look['image_asset_id'], 'source_asset_id': look['source_asset_id'],
        'selected': look['selected_order'] is not None, 'selected_order': look['selected_order'],
        'usable': problem is None,
        'unusable_reason': LOOK_PROBLEM_MESSAGES[problem] if problem not in (None, 'not_ready') else None,
        'face_score': look['face_score'], 'created_at': look['created_at'],
    }


def style_name(style_id: str) -> str:
    return STYLE_BY_ID.get(style_id, {}).get('name', style_id)
