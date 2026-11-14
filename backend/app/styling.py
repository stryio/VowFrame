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

V7.22: the groom's look is two edits as well: his retouched photo (a makeup row with role 'groom' and no style,
shared by every style like his looks), then the hair on it.

V7.21: what a makeup or look shows depends only on what it was made from, not on the style that uses it, so
another style can reuse one (reuse_makeup): it gets its own rows on the same images, with no new edit, and every
per-style rule above (selection, deletion, auto pick) works on them unchanged. makeup_style_id records the style
whose colour family and finish the makeup was painted in, which the side looks and the shots then describe.

A shot task records its looks in its model snapshot:
    'looks': {'version': STYLING_VERSION, 'bride': look id, 'groom': look id}
    'reference_slots': one [role, angle] per reference image, null for a final look or a sample reference
The task's reference images hold the front looks from the start; a side photo the shot needs is replaced by its
side look (derived from the side photo and the front look) when the task runs, so a replay ('same') reuses
exactly the images the original used.

V4.0 tasks recorded 'styling' instead and styled each photo per style when they ran; they still replay, but that
automatic styling is retired.
"""

from pathlib import Path

from PIL import Image

from .db import new_id, utc_now
from .facings import SIDE_FACINGS, direction, upload_column
from .face_swap import face_swap_reference_ids
from .retouch import candidate_retouch_settings, has_face_shaping
from .prompts import (HAIRSTYLE_BY_KEY, JEWELRY_BY_ID, MAKEUP_COLOR_BY_ID, STYLE_BY_ID, STYLE_MAKEUP_COLORS, STYLE_OUTFITS, STYLES, STYLING_VERSION,
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
    """Why a makeup candidate cannot be used for a new look of its person; None when it can (V7.8)."""
    return _candidate_problem(project, makeup, makeup['role'])


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
    'photo_replaced': '这个人的照片已经更换，这张妆容不能再用于新造型',
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
    # V7.22: a groom look made on a retouched photo differs from its siblings only in the hair, as the bride's do.
    scored = [row for row in rows if row['face_score'] is not None
              and not (row['role'] == 'groom' and row['makeup_id'] is None and has_face_shaping(row))]
    best = max(scored, key=lambda row: row['face_score']) if scored else (current or (rows[0] if rows else None))
    if best is not None and (current is None or best['id'] != current['id']):
        select_only(conn, best, 'auto')


def _makeup_group(makeup) -> tuple[str, tuple]:
    """SQL condition and parameters for the makeups that compete with this one: same person, same style (the
    groom's: all of his, V7.22)."""
    if makeup['style_id'] is None:
        return ('project_id=? AND person=? AND style_id IS NULL AND discarded_at IS NULL', (makeup['project_id'], makeup['person']))
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

    A group containing a slimming edit is never ranked by likeness, since that would favour a request that
    changes the face less. Legacy candidates inherit this rule from their recorded preset."""
    condition, params = _makeup_group(makeup)
    rows = [row for row in conn.execute(f'SELECT * FROM makeups WHERE {condition} ORDER BY rowid', params)
            if makeup_problem(project, row) is None]
    current = next((row for row in rows if row['selected_by'] is not None), None)
    if current is not None and current['selected_by'] != 'auto':
        return
    # With mixed settings, a higher likeness can just mean that less slimming was requested.
    # Keep the current/first ready candidate whenever this group includes a slimming edit.
    scored = [] if any(has_face_shaping(row) for row in rows) else [row for row in rows if row['face_score'] is not None]
    best = max(scored, key=lambda row: row['face_score']) if scored else (current or (rows[0] if rows else None))
    if best is not None and (current is None or best['id'] != current['id']):
        select_makeup(conn, best, 'auto')


def selected_makeup(conn, project, role: str, style_id: str | None):
    """The selected makeup of this person for this style (the groom's: style_id None), if it can still be used."""
    style_clause = 'style_id IS NULL' if style_id is None else 'style_id=?'
    row = conn.execute(
        f'SELECT * FROM makeups WHERE project_id=? AND person=? AND {style_clause} AND selected_by IS NOT NULL '
        'AND discarded_at IS NULL ORDER BY rowid DESC LIMIT 1',
        (project['id'], person_of(project, role), *(() if style_id is None else (style_id,)))).fetchone()
    return row if row is not None and makeup_problem(project, row) is None else None


def makeup_style_of(row) -> str | None:
    """The style whose colour family and finish a bride makeup or look wears (V7.21); None for the groom's looks."""
    return row['makeup_style_id'] or row['style_id']


def _copy_row(conn, table: str, row, **changes) -> str:
    """Insert a copy of a row with some columns changed; returns the copy's id."""
    values = {key: row[key] for key in row.keys()} | changes
    conn.execute(f'INSERT INTO {table} ({",".join(values)}) VALUES ({",".join("?" * len(values))})', tuple(values.values()))
    return values['id']


def reuse_makeup(conn, project, makeup, style_id: str) -> str:
    """Use a ready makeup of another style for this style too (V7.21), without a new edit; returns the id of the
    style's row for it, which becomes the style's selected makeup.

    The style gets its own copy of the makeup (or keeps the one it already has on the same image), and a copy of
    every usable front look made on that image in any style, with its side looks, unless the style already has a
    look on the same image or the hairstyle does not fit the style (an updo style). The copies keep the style the
    makeup was painted in (makeup_style_id), so the shots describe the makeup the pictures show. A copied look
    shares the side looks of its image (V7.31), made before or after the reuse."""
    painted = makeup_style_of(makeup)
    makeup_style = painted if painted != style_id else None
    person, now = makeup['person'], utc_now()
    own = conn.execute('SELECT id FROM makeups WHERE project_id=? AND person=? AND style_id=? AND image_asset_id=? '
                       'AND discarded_at IS NULL ORDER BY rowid LIMIT 1',
                       (project['id'], person, style_id, makeup['image_asset_id'])).fetchone()
    target_id = own['id'] if own is not None else _copy_row(
        conn, 'makeups', makeup, id=new_id(), style_id=style_id, makeup_style_id=makeup_style, selected_by=None,
        client_request_id=None, created_at=now)
    have = {row['image_asset_id'] for row in conn.execute(
        "SELECT image_asset_id FROM looks WHERE project_id=? AND person=? AND style_id=? AND angle='front' AND discarded_at IS NULL",
        (project['id'], person, style_id))}
    last = None
    for look in conn.execute("SELECT * FROM looks WHERE project_id=? AND person=? AND angle='front' AND makeup_asset_id=? "
                             'AND discarded_at IS NULL ORDER BY rowid', (project['id'], person, makeup['image_asset_id'])).fetchall():
        if (look['image_asset_id'] in have or look_problem(project, look) is not None
                or not fits_style('bride', look['hairstyle_id'], look['hair_length'], style_id)):
            continue
        have.add(look['image_asset_id'])
        last = _copy_row(conn, 'looks', look, id=new_id(), style_id=style_id, makeup_id=target_id, makeup_style_id=makeup_style,
                         selected_order=None, selected_by=None, client_request_id=None, created_at=now)
    target = conn.execute('SELECT * FROM makeups WHERE id=?', (target_id,)).fetchone()
    select_makeup(conn, target)
    if last is not None:
        auto_select(conn, project, conn.execute('SELECT * FROM looks WHERE id=?', (last,)).fetchone())
    return target_id


def makeup_response(project, makeup) -> dict:
    color = MAKEUP_COLOR_BY_ID.get(makeup['makeup_color'])
    problem = makeup_problem(project, makeup)
    return {
        'id': makeup['id'], 'person': makeup['person'], 'role': makeup['role'], 'style_id': makeup['style_id'],
        'makeup_style_id': makeup_style_of(makeup),
        'style_name': style_name(makeup['style_id']) if makeup['style_id'] else None,
        'makeup_style_name': style_name(makeup_style_of(makeup)) if makeup_style_of(makeup) else None,
        'makeup_color': makeup['makeup_color'], 'makeup_color_name': color.name if color else None,
        'makeup_level': makeup['makeup_level'],
        'retouch_settings': candidate_retouch_settings(makeup),
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


def side_by_side(pictures: list[Path], out: Path) -> Path:
    """Pictures in one row, left to right, at one height with a white gap between: the two looks, the bride on the
    left (V7.41), or the side looks a shot needs, in reference order (V7.43)."""
    rows = []
    for path in pictures:
        with Image.open(path) as source:
            rows.append(source.convert('RGB'))
    height = min(1536, max(picture.height for picture in rows))
    rows = [picture.resize((round(picture.width * height / picture.height), height), Image.LANCZOS) for picture in rows]
    gap = 32
    canvas = Image.new('RGB', (sum(picture.width for picture in rows) + gap * (len(rows) - 1), height), 'white')
    left = 0
    for picture in rows:
        canvas.paste(picture, (left, 0))
        left += picture.width + gap
    canvas.save(out)
    return out


def look_snapshot(bride_look_id: str, groom_look_id: str) -> dict:
    return {'version': STYLING_VERSION, 'bride': bride_look_id, 'groom': groom_look_id}


def reference_slots(side_references: list[tuple[str, str]], with_preview: bool, art: bool = False) -> list:
    """[role, angle] for each image of app.api._build_references, in its fixed order. Images that are no face source
    (the sample, the style art of V7.33, and the final looks of V7.18 pictures) have no slot, so side looks and face
    swaps leave them alone. The style art comes last and may be added only when the task runs."""
    slots = [['bride', 'front'], ['groom', 'front']]
    slots.extend([role, facing] for role, facing in side_references)
    if with_preview or art:
        slots.append(None)
    return slots


def current_side_looks(conn, project) -> list[dict]:
    """For each shown front look and each current side photo, its latest side look (V7.24): the one a new shot uses
    when ready; since V7.30 also one queued, being made or failed after the user asked for it."""
    result = []
    for front in conn.execute("SELECT * FROM looks WHERE project_id=? AND angle='front' AND discarded_at IS NULL "
                              'ORDER BY created_at,rowid', (project['id'],)):
        for facing in SIDE_FACINGS:
            upload = project[upload_column(front['person'], facing)]
            if not upload:
                continue
            angle = direction(facing)
            row = latest_side_look(conn, front['id'], angle, upload)
            if row is not None:
                result.append({'id': row['id'], 'parent_look_id': front['id'], 'person': row['person'], 'role': row['role'],
                               'angle': angle, 'facing': facing, 'asset_id': row['image_asset_id'], 'source_asset_id': upload,
                               'status': row['status'], 'error_message': row['error_message'], 'created_at': row['created_at'],
                               'face_score': row['face_score']})
    return result


# V7.31: a side look shows only what it was made from, the side photo and the front look's image, so it belongs to
# that image, not to one look row: a look another style reuses (a copy on the same image) shares its side looks.
_SIDE_LOOKS_OF_IMAGE = (
    'SELECT side.* FROM looks side JOIN looks front ON front.id=side.parent_look_id '
    'WHERE front.image_asset_id=(SELECT image_asset_id FROM looks WHERE id=?) AND side.angle=? AND side.source_asset_id=? '
    'AND side.discarded_at IS NULL')


def side_look_key(conn, parent_look_id: str, angle: str, source_asset_id: str) -> tuple:
    """What makes two side looks the same picture (V7.31): the front look's image, the angle and the side photo."""
    row = conn.execute('SELECT image_asset_id FROM looks WHERE id=?', (parent_look_id,)).fetchone()
    return (row['image_asset_id'] if row else parent_look_id, angle, source_asset_id)


def latest_side_look(conn, parent_look_id: str, angle: str, source_asset_id: str):
    """The newest side look of this front look's image, angle and side photo that is not deleted: a ready one
    first, else the newest queued, running or failed one (V7.30)."""
    return conn.execute(_SIDE_LOOKS_OF_IMAGE + " ORDER BY side.status='ready' DESC,side.created_at DESC,side.rowid DESC LIMIT 1",
                        (parent_look_id, angle, source_asset_id)).fetchone()


def find_side_look(conn, parent_look_id: str, angle: str, source_asset_id: str) -> str | None:
    """Image asset of the side look made from this side photo and front look's image; None when there is none yet."""
    row = conn.execute(_SIDE_LOOKS_OF_IMAGE + " AND side.status='ready' ORDER BY side.created_at DESC,side.rowid DESC LIMIT 1",
                       (parent_look_id, angle, source_asset_id)).fetchone()
    return row['image_asset_id'] if row else None


def discard_side_look(conn, side, now: str):
    """Delete a side look, with any other finished copy of the same picture (an older reuse copied them, V7.21),
    so a copy does not show up again in its place."""
    same = _SIDE_LOOKS_OF_IMAGE.replace('SELECT side.*', 'SELECT side.id', 1)
    conn.execute(f"UPDATE looks SET discarded_at=? WHERE id=? OR id IN ({same} "
                 "AND side.status IN ('ready','failed','interrupted'))",
                 (now, side['id'], side['parent_look_id'], side['angle'], side['source_asset_id']))


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
            # V7.48: a full side the shot used stands for its direction too.
            used[(slot[0], direction(slot[1]))] = asset_id
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
        'jewelry_id': look['jewelry_id'], 'jewelry_name': JEWELRY_BY_ID[look['jewelry_id']].name if look['jewelry_id'] in JEWELRY_BY_ID else None,
        'makeup_color': look['makeup_color'], 'makeup_color_name': color.name if color else None,
        'makeup_level': look['makeup_level'], 'hair_length': look['hair_length'],
        'retouch_settings': candidate_retouch_settings(look),
        'status': look['status'], 'error_code': look['error_code'], 'error_message': look['error_message'],
        'asset_id': look['image_asset_id'], 'source_asset_id': look['source_asset_id'],
        'selected': look['selected_order'] is not None, 'selected_order': look['selected_order'],
        'usable': problem is None,
        'unusable_reason': LOOK_PROBLEM_MESSAGES[problem] if problem not in (None, 'not_ready') else None,
        'face_score': look['face_score'], 'created_at': look['created_at'],
        'makeup_asset_id': look['makeup_asset_id'], 'makeup_face_score': look['makeup_face_score'],
        'selected_by': look['selected_by'], 'makeup_id': look['makeup_id'],
        'makeup_style_id': makeup_style_of(look) if look['role'] == 'bride' else None,
    }


def style_name(style_id: str) -> str:
    return STYLE_BY_ID.get(style_id, {}).get('name', style_id)
