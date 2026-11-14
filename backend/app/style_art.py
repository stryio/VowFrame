"""Style art (V7.33, docs/prompts.md section 12): one picture per live style of its outfit, worn by no one, standing
in the style's main scene. Shots copy the clothes and the place from it.

The art is shared by every project. It is made the first time a shot needs it (or when the user asks for it again),
in three edits: the outfit on white, the empty scene, and the two put together.

V7.34: a style with rooms (app/rooms.py) has one art per area of each room. The empty scene is then the area's
picture from the repository (no edit), and the outfit on white is shared by all of the outfit's arts, so every
area wears the same clothes and a new area costs one edit, the combination. A project's task refers to the art
through an image_assets row of its own whose file path is the shared file, so every per-project rule (asset lookup,
stale checks, deleting a project) keeps working and never removes the shared file.
"""

from pathlib import Path

from .config import Config
from .db import new_id
from .prompts import STYLE_ART_ASPECT_RATIO, STYLE_ART_COMBINE_ASPECT_RATIO, STYLE_OUTFITS, has_style_art, style_art_version
from .rooms import room_image_version
from .storage import asset_path

STYLE_ART_ASSET_TYPE = 'style_art'
# V7.35: outfit-on-white pictures made and checked before an outfit went live; an art of such an outfit starts from
# this file instead of a paid edit.
OUTFIT_IMAGE_DIR = Path(__file__).resolve().parent / 'outfit_images'


def outfit_product_image(outfit_id: str) -> Path | None:
    path = OUTFIT_IMAGE_DIR / f'{outfit_id}.jpg'
    return path if path.is_file() else None


def default_outfit(style_id: str) -> str:
    return STYLE_OUTFITS[style_id][0]


def style_art_applies(db, style_id: str, outfit: str | None) -> bool:
    """Whether a new shot of this style is made from its style art: a live style, a recorded outfit, the setting on."""
    return outfit is not None and has_style_art(style_id) and db.get_style_art_mode() == 'on'


_KEY = ('style_id=? AND outfit_id=? AND art_version=? AND room_id IS ? AND area IS ? AND scene_version IS ? '
        'AND COALESCE(aspect_ratio,?)=?')


def art_area(room: str | None, area: int | None) -> int | None:
    """The area whose art a shot uses: since V7.50 a room has one art, of its main picture (area 1), whatever area the
    shot stands in; None without a room."""
    return 1 if room is not None else None


def _key(style_id, outfit_id, room, area):
    """V7.38: a room area's art also belongs to the room's pictures it was made from (ROOM_IMAGE_VERSION; V7.57: per room, room_image_version). V7.49: and
    has its shape; arts from before had none and were all STYLE_ART_ASPECT_RATIO. V7.52: every new art is
    STYLE_ART_COMBINE_ASPECT_RATIO, so the 16:9 and 4:3 arts of before are made again."""
    area = art_area(room, area)
    return (style_id, outfit_id, style_art_version(outfit_id), room, area, room_image_version(style_id, room) if room is not None else None,
            STYLE_ART_ASPECT_RATIO, STYLE_ART_COMBINE_ASPECT_RATIO)


def current_style_art(conn, style_id: str, outfit_id: str, room: str | None = None, area: int | None = None):
    """The art new shots use: the latest ready one of this version that was not thrown away. room and area: the
    room area it shows (V7.34); None for a style's single main-scene art."""
    return conn.execute(
        f"SELECT * FROM style_arts WHERE {_KEY} AND status='ready' AND discarded_at IS NULL "
        'ORDER BY created_at DESC,rowid DESC LIMIT 1', _key(style_id, outfit_id, room, area)).fetchone()


def latest_style_art(conn, style_id: str, outfit_id: str, room: str | None = None, area: int | None = None):
    """The latest attempt of this version, whatever became of it (what the style art page shows)."""
    return conn.execute(
        f'SELECT * FROM style_arts WHERE {_KEY} AND discarded_at IS NULL ORDER BY created_at DESC,rowid DESC LIMIT 1',
        _key(style_id, outfit_id, room, area)).fetchone()


def failed_style_art(conn, style_id: str, outfit_id: str, room: str | None = None, area: int | None = None):
    """The latest failed or interrupted attempt, whose finished steps the next attempt reuses."""
    return conn.execute(
        f"SELECT * FROM style_arts WHERE {_KEY} AND status IN ('failed','interrupted') AND discarded_at IS NULL "
        'ORDER BY created_at DESC,rowid DESC LIMIT 1', _key(style_id, outfit_id, room, area)).fetchone()


def shared_product(conn, config: Config, outfit_id: str) -> str | None:
    """The outfit-on-white picture the outfit's room arts share (V7.34): the one of its latest ready art, else of
    its latest attempt that made one; None when there is none yet."""
    rows = conn.execute(
        "SELECT product_path FROM style_arts WHERE outfit_id=? AND art_version=? AND product_path IS NOT NULL "
        "AND discarded_at IS NULL ORDER BY status='ready' DESC,created_at DESC,rowid DESC", (outfit_id, style_art_version(outfit_id)))
    return next((row['product_path'] for row in rows if asset_path(config, row['product_path']).is_file()), None)


def art_files_present(config: Config, art) -> bool:
    return all(art[column] and asset_path(config, art[column]).is_file()
               for column in ('product_path', 'scene_path', 'image_path'))


def project_art_asset(conn, project_id: str, art, now: str) -> str:
    """This project's image asset of the art's picture, made the first time the project uses it."""
    row = conn.execute('SELECT id FROM image_assets WHERE project_id=? AND file_path=? AND asset_type=?',
                       (project_id, art['image_path'], STYLE_ART_ASSET_TYPE)).fetchone()
    if row is not None:
        return row['id']
    asset_id = new_id()
    conn.execute('INSERT INTO image_assets (id,project_id,file_path,asset_type,created_at) VALUES (?,?,?,?,?)',
                 (asset_id, project_id, art['image_path'], STYLE_ART_ASSET_TYPE, now))
    return asset_id


def ready_art_asset(conn, config: Config, project_id: str, style_id: str, outfit_id: str, now: str,
                    room: str | None = None, area: int | None = None) -> str | None:
    """The project's asset of the style's ready art when there is one; None leaves the art to the task."""
    art = current_style_art(conn, style_id, outfit_id, room, area)
    if art is None or not art_files_present(config, art):
        return None
    return project_art_asset(conn, project_id, art, now)


def style_art_snapshot(style_id: str, outfit_id: str, room: str | None = None, area: int | None = None) -> dict:
    snapshot = {'style': style_id, 'outfit': outfit_id, 'version': style_art_version(outfit_id)}
    if room is not None:
        snapshot.update(room=room, area=art_area(room, area))
    return snapshot


def art_pending(references: list, snapshot: dict) -> bool:
    """A task made before its style's art was ready: the art's slot is the last one and has no image yet."""
    return bool(snapshot.get('style_art')) and len(references) < len(snapshot.get('reference_slots') or [])
