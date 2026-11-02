"""Looks (V5.0): styled photos the user generates and picks, and which look a shot uses.

The image edit endpoint copies what the reference photos show, so a hairstyle cannot be asked of a shot. Instead
each person's uploaded photo is edited into "looks" when the user asks for candidates, one edit per hairstyle;
the user selects some, and every sample is shot from one selected look per person. Photo sets and slot actions
keep the looks of the picture they start from.

V6.0–V6.4: a look was only the hairstyle on the uploaded face, and the shot drew the makeup.

V7.0: looks come after the style. The bride has looks per style (style_id), each with hair and makeup in the
style's colour family and finish; the groom's looks (style_id NULL) are shared by every style. Each (person,
style) group has at most one selected look, and that is the look a new sample of the style uses. Looks of older
versions are not used for new shots.

V7.8: the bride's makeup for a style is its own candidates (the makeups table): makeup edits of the uploaded
photo, hair unchanged, of which one is selected per style. Every new bride look of the style is a hair edit of
that made-up photo, so its hair candidates differ only in the hair. V7.5–V7.7 looks made their own makeup each.

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
from .prompts import (FACE_SHAPING_LEVELS, HAIRSTYLE_BY_KEY, MAKEUP_COLOR_BY_ID, STYLE_BY_ID, STYLE_MAKEUP_COLORS, STYLE_OUTFITS, STYLES, STYLING_VERSION,
                      UPDO_STYLES)

# Order of face_swap_reference_ids().
FACE_SWAP_SLOTS = (('bride', 'front'), ('groom', 'front'), ('bride', 'left'), ('bride', 'right'),
                   ('groom', 'left'), ('groom', 'right'))
# Hairstyles suggested first, in order (V5.2): the groom's are checked as candidates; a bride look is made per
# style, so only the first that fits the style is suggested (V7.0). A hairstyle the stated hair length cannot do
# is replaced by the next one of its length list that is unused.
DEFAULT_LOOKS = {
    'bride': ('keep', 'low_bun', 'soft_waves', 'half_up', 'high_bun', 'french_bun'),
    'groom': ('keep', 'side_part', 'textured', 'side_back'),
}
# Replacements for a bride with short hair, in order.
SHORT_BRIDE_HAIRSTYLES = ('slick_short', 'short_inward', 'short_curls')


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


def default_look_items(project, role: str) -> list[str]:
    """The default hairstyles for this person's stated hair length, without duplicates.

    A hairstyle the length cannot do becomes the next unused short-hair style (a bride with short hair), else the
    photo's own hairstyle; a groom with a buzz cut only keeps his own.
    """
    length = hair_length_of(project, role)
    spare = [item for item in SHORT_BRIDE_HAIRSTYLES if role == 'bride' and length in HAIRSTYLE_BY_KEY[(role, item)].lengths]
    items = []
    for hairstyle_id in DEFAULT_LOOKS[role]:
        if length not in HAIRSTYLE_BY_KEY[(role, hairstyle_id)].lengths:
            hairstyle_id = spare.pop(0) if spare else 'keep'
        if hairstyle_id not in items:
            items.append(hairstyle_id)
    return items


def _candidate_problem(project, row, role: str) -> str | None:
    """Why a look or makeup candidate of this role cannot be used any more; None when it can."""
    if row['discarded_at'] is not None:
        return 'unavailable'
    if row['status'] != 'ready' or row['image_asset_id'] is None:
        return 'not_ready'
    if row['styling_version'] != STYLING_VERSION:
        return 'outdated'
    if role_of(project, row['person']) != role:
        return 'role_changed'
    if project[f"person_{row['person']}_image_id"] != row['source_asset_id']:
        return 'photo_replaced'
    return None


def look_problem(project, look) -> str | None:
    """Why a front look cannot be used for a new shot; None when it can."""
    if look['angle'] != 'front':
        return 'unavailable'
    return _candidate_problem(project, look, look['role'])


def makeup_problem(project, makeup) -> str | None:
    """Why a makeup candidate cannot be used for a new bride look; None when it can (V7.8)."""
    return _candidate_problem(project, makeup, 'bride')


LOOK_PROBLEM_MESSAGES = {
    'unavailable': '这套造型不存在或已删除',
    'outdated': '这是旧版本的造型，没有按风格化妆；现在新娘每个风格做一套带妆的造型，请重新生成',
    'not_ready': '这套造型还没有生成好',
    'role_changed': '交换新娘和新郎之后，这套造型不能再用于新照片',
    'photo_replaced': '这个人的照片已经更换，这套造型不能再用于新照片',
}


MAKEUP_PROBLEM_MESSAGES = {
    'unavailable': '这张妆容不存在或已删除',
    'outdated': '这是旧版本的妆容，请重新生成',
    'not_ready': '这张妆容还没有生成好',
    'role_changed': '交换新娘和新郎之后，这张妆容不能再用于新造型',
    'photo_replaced': '新娘的照片已经更换，这张妆容不能再用于新造型',
}


def fits_style(role: str, hairstyle_id: str, hair_length: str, style_id: str) -> bool:
    """The Chinese styles need the bride in an updo; a bride with short hair keeps her own hairstyle."""
    if style_id not in UPDO_STYLES or role != 'bride':
        return True
    return HAIRSTYLE_BY_KEY[('bride', hairstyle_id)].updo or (hair_length == 'short' and hairstyle_id == 'keep')


def bride_look_color(style_id: str) -> str:
    """The colour family a bride look for this style wears: the style's recommended one."""
    return STYLE_MAKEUP_COLORS[style_id][0]


def selected_look(conn, project, role: str, style_id: str | None):
    """The selected look of this person for this style (the groom's: style_id None), if it can still be used."""
    style_clause = 'style_id IS NULL' if style_id is None else 'style_id=?'
    row = conn.execute(
        f"SELECT * FROM looks WHERE project_id=? AND person=? AND angle='front' AND {style_clause} "
        'AND selected_order IS NOT NULL AND discarded_at IS NULL ORDER BY rowid DESC LIMIT 1',
        (project['id'], person_of(project, role), *(() if style_id is None else (style_id,)))).fetchone()
    return row if row is not None and look_problem(project, row) is None else None


def _group(look) -> tuple[str, tuple]:
    """SQL condition and parameters for the looks that compete with this one: same person, same style (or groom)."""
    style_clause = 'style_id IS NULL' if look['style_id'] is None else 'style_id=?'
    return (f"project_id=? AND person=? AND angle='front' AND discarded_at IS NULL AND {style_clause}",
            (look['project_id'], look['person'], *(() if look['style_id'] is None else (look['style_id'],))))


def select_only(conn, look, by: str = 'user'):
    """Make this look the one its person uses for its style (or, for the groom, for every style). by: 'user' when
    the user picked it, 'auto' when it was picked as the most similar (V7.5)."""
    condition, params = _group(look)
    conn.execute(f'UPDATE looks SET selected_order=NULL,selected_by=NULL WHERE {condition}', params)
    conn.execute('UPDATE looks SET selected_order=1,selected_by=? WHERE id=?', (by, look['id']))


def auto_select(conn, project, look):
    """Pick the look its group uses unless the user picked one (V7.5): the most similar usable look by its likeness
    score to the uploaded photo, or the first ready one while none is scored (no FaceFusion, or not scored yet)."""
    condition, params = _group(look)
    rows = [row for row in conn.execute(f'SELECT * FROM looks WHERE {condition} ORDER BY rowid', params)
            if look_problem(project, row) is None]
    current = next((row for row in rows if row['selected_order'] is not None), None)
    if current is not None and current['selected_by'] != 'auto':
        return
    # V7.15: a groom look is one edit, so at a level that reshapes the face the score would favour the one that did
    # the least of it, as for the bride's makeups. Her looks of a style share one makeup and differ only in the hair.
    scored = [row for row in rows if row['face_score'] is not None
              and not (row['role'] == 'groom' and row['makeup_level'] in FACE_SHAPING_LEVELS)]
    best = max(scored, key=lambda row: row['face_score']) if scored else (current or (rows[0] if rows else None))
    if best is not None and (current is None or best['id'] != current['id']):
        select_only(conn, best, 'auto')


def _makeup_group(makeup) -> tuple[str, tuple]:
    """SQL condition and parameters for the makeups that compete with this one: same person, same style."""
    return ('project_id=? AND person=? AND style_id=? AND discarded_at IS NULL',
            (makeup['project_id'], makeup['person'], makeup['style_id']))


def select_makeup(conn, makeup, by: str = 'user'):
    """Make this the makeup new bride looks of its style are made on (V7.8). by: as select_only."""
    condition, params = _makeup_group(makeup)
    conn.execute(f'UPDATE makeups SET selected_by=NULL WHERE {condition}', params)
    conn.execute('UPDATE makeups SET selected_by=? WHERE id=?', (by, makeup['id']))


def auto_select_makeup(conn, project, makeup):
    """Pick the makeup of its style unless the user picked one, as auto_select does for looks: the most similar
    usable one by its likeness score, or the first ready one while none is scored.

    V7.10: a level that reshapes the face a little (FACE_SHAPING_LEVELS) is never picked by score, since the score
    would favour the candidate that did the least of it; the first ready one stays until the user picks."""
    condition, params = _makeup_group(makeup)
    rows = [row for row in conn.execute(f'SELECT * FROM makeups WHERE {condition} ORDER BY rowid', params)
            if makeup_problem(project, row) is None]
    current = next((row for row in rows if row['selected_by'] is not None), None)
    if current is not None and current['selected_by'] != 'auto':
        return
    scored = [row for row in rows if row['face_score'] is not None and row['makeup_level'] not in FACE_SHAPING_LEVELS]
    best = max(scored, key=lambda row: row['face_score']) if scored else (current or (rows[0] if rows else None))
    if best is not None and (current is None or best['id'] != current['id']):
        select_makeup(conn, best, 'auto')


def selected_makeup(conn, project, style_id: str):
    """The bride's selected makeup for this style, if it can still be used."""
    row = conn.execute(
        'SELECT * FROM makeups WHERE project_id=? AND person=? AND style_id=? AND selected_by IS NOT NULL '
        'AND discarded_at IS NULL ORDER BY rowid DESC LIMIT 1', (project['id'], person_of(project, 'bride'), style_id)).fetchone()
    return row if row is not None and makeup_problem(project, row) is None else None


def makeup_response(project, makeup) -> dict:
    color = MAKEUP_COLOR_BY_ID.get(makeup['makeup_color'])
    problem = makeup_problem(project, makeup)
    return {
        'id': makeup['id'], 'person': makeup['person'], 'style_id': makeup['style_id'],
        'makeup_color': makeup['makeup_color'], 'makeup_color_name': color.name if color else None,
        'makeup_level': makeup['makeup_level'],
        'status': makeup['status'], 'error_code': makeup['error_code'], 'error_message': makeup['error_message'],
        'asset_id': makeup['image_asset_id'], 'source_asset_id': makeup['source_asset_id'],
        'selected': makeup['selected_by'] is not None, 'selected_by': makeup['selected_by'],
        'usable': problem is None,
        'unusable_reason': MAKEUP_PROBLEM_MESSAGES[problem] if problem not in (None, 'not_ready') else None,
        'face_score': makeup['face_score'], 'created_at': makeup['created_at'],
    }


def style_looks(conn, project) -> dict:
    """Per style, the looks a new sample uses: the bride's selected look for it and the groom's selected look."""
    groom = selected_look(conn, project, 'groom', None)
    result = {}
    for style in STYLES:
        bride = selected_look(conn, project, 'bride', style['id'])
        result[style['id']] = {'bride': bride['id'] if bride else None, 'groom': groom['id'] if groom else None}
    return result


def style_outfits(conn, project) -> dict[str, str]:
    """Per style with outfits, the outfit a new sample wears (V7.17): the project's choice, else the style's first."""
    chosen = {row['style_id']: row['outfit_id'] for row in conn.execute(
        'SELECT style_id,outfit_id FROM project_outfits WHERE project_id=?', (project['id'],))}
    return {style_id: chosen[style_id] if chosen.get(style_id) in outfits else outfits[0]
            for style_id, outfits in STYLE_OUTFITS.items()}


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
        'id': look['id'], 'person': look['person'], 'role': look['role'], 'style_id': look['style_id'],
        'hairstyle_id': look['hairstyle_id'], 'hairstyle_name': hairstyle.name if hairstyle else look['hairstyle_id'],
        'makeup_color': look['makeup_color'], 'makeup_color_name': color.name if color else None,
        'makeup_level': look['makeup_level'], 'hair_length': look['hair_length'],
        'status': look['status'], 'error_code': look['error_code'], 'error_message': look['error_message'],
        'asset_id': look['image_asset_id'], 'source_asset_id': look['source_asset_id'],
        'selected': look['selected_order'] is not None, 'selected_order': look['selected_order'],
        'usable': problem is None,
        'unusable_reason': LOOK_PROBLEM_MESSAGES[problem] if problem not in (None, 'not_ready') else None,
        'face_score': look['face_score'], 'created_at': look['created_at'],
        'makeup_asset_id': look['makeup_asset_id'], 'makeup_face_score': look['makeup_face_score'],
        'selected_by': look['selected_by'], 'makeup_id': look['makeup_id'],
    }


def style_name(style_id: str) -> str:
    return STYLE_BY_ID.get(style_id, {}).get('name', style_id)
