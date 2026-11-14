"""AI face fix (V7.1; the whole picture since V7.2): make both faces in a finished picture look like the couple,
keeping hair, makeup and retouching (docs/prompts.md section 9). Since V7.32 the same 4K edit also fixes the hands
and washes the picture, with the wash model.

FaceFusion's swap re-renders the face from the uploaded photo, and the bare-faced photo washes the makeup out.
Here the image model makes the likeness edit: one edit of the whole picture with both looks (V7.4; the uploaded
photos before, whose bare skin and old hair the edit copied onto the faces). V7.78 (the user: AI修脸改成只修脸): new
tasks edit only the faces again, at the configured size (no 4K); tasks recorded with 'retouch' (V7.32–V7.77) keep the hands and the wash.
V7.9–V7.13 used the bride's made-up
photo before the hair edit, and the edit copied its old hair even though the prompt said not to; V7.14 is back to her
look. Tasks recorded as 'makeup' keep their prompt.
V7.13 split mode edits only the bride, using the picture and her reference; a local FaceFusion task then swaps
only the groom. Manual face fixes and the explicit 'ai' mode continue editing both people.

V7.1 edited a crop around each face and laid only the face back. The laid-back face did not belong to the head
around it (a different expression and light inside the same outline), and looked like a pasted meme face, so the
whole edit is kept as it comes back.
"""

import json

from .prompts import with_hands_and_wash, FACE_FIX_BRIDE_MAKEUP_PROMPT, FACE_FIX_BRIDE_PROMPT, FACE_FIX_GROOM_PROMPT, FACE_FIX_MAKEUP_PROMPT, FACE_FIX_PROMPT, HAND_FIX_PROMPT, WASH_PROMPT


ROLES = ('bride', 'groom')


def face_fix_snapshot(model: str, aspect_ratio: str, *, roles: list[str] | None = None, swap_roles: list[str] | None = None,
                      provider: str | None = None) -> str:
    """Model record stored on face-fix tasks: the edit keeps the picture's frame. bride_reference: 'look' (V7.14);
    tasks from V7.9–V7.13 may say 'makeup', and tasks older than V7.9 have no key and used the look.
    roles: who the edit corrects, recorded only when it is one person (V7.13 the bride, V7.29 either). swap_roles
    (V7.29): who FaceFusion swaps afterwards ('then' 'swap'); V7.13–V7.28 tasks say 'then' 'groom_swap'.
    retouch (V7.32–V7.77 only): the edit also fixed the hands and washed the picture. resolution: 4K on V7.32–V7.77 tasks only."""
    # V7.78: only the faces, at the configured size: no 'retouch' (no hand or wash paragraphs) and no 'resolution'.
    snapshot = {'generator': 'face_fix', 'model': model, 'aspect_ratio': aspect_ratio, 'bride_reference': 'look'}
    if provider:
        snapshot['provider'] = provider  # V7.27: the service the edit goes to; older tasks follow Settings
    if roles and set(roles) != set(ROLES):
        snapshot['roles'] = list(roles)
    if swap_roles:
        snapshot.update(then='swap', swap_roles=list(swap_roles))
    return json.dumps(snapshot, ensure_ascii=False)


def fix_roles(snapshot: dict) -> list[str]:
    """Who a face-fix task corrects: both unless it records one person."""
    return list(snapshot.get('roles') or ROLES)


def follow_up_swap_roles(snapshot: dict) -> list[str]:
    """Who FaceFusion swaps after this face fix (V7.13 the groom, V7.29 anyone); empty when nobody."""
    if snapshot.get('then') == 'groom_swap':
        return ['groom']
    return list(snapshot.get('swap_roles') or []) if snapshot.get('then') == 'swap' else []


def face_fix_prompt(snapshot: dict) -> str:
    """The edit prompt for the references and the people the task recorded, with the hands and the wash only for
    tasks that recorded them (V7.32–V7.77)."""
    makeup = snapshot.get('bride_reference') == 'makeup'
    if snapshot.get('roles') == ['groom']:
        prompt = FACE_FIX_GROOM_PROMPT
    elif snapshot.get('roles') == ['bride']:
        prompt = FACE_FIX_BRIDE_MAKEUP_PROMPT if makeup else FACE_FIX_BRIDE_PROMPT
    else:
        prompt = FACE_FIX_MAKEUP_PROMPT if makeup else FACE_FIX_PROMPT
    return with_hands_and_wash(prompt) if snapshot.get('retouch') else prompt


def hand_fix_snapshot(model: str, aspect_ratio: str, provider: str | None = None) -> str:
    """Model record stored on hand-fix tasks (V7.25): one edit of the picture alone, in its own frame."""
    snapshot = {'generator': 'hand_fix', 'model': model, 'aspect_ratio': aspect_ratio}
    return json.dumps({**snapshot, 'provider': provider} if provider else snapshot, ensure_ascii=False)


def wash_snapshot(model: str, aspect_ratio: str, provider: str | None = None) -> str:
    """Model record stored on wash tasks (V7.26): one edit of the picture alone, asked for in 4K."""
    snapshot = {'generator': 'wash', 'model': model, 'aspect_ratio': aspect_ratio, 'resolution': '4K'}
    return json.dumps({**snapshot, 'provider': provider} if provider else snapshot, ensure_ascii=False)


def fix_prompt(mode: str, snapshot: dict) -> str:
    """The edit prompt of an AI fix task: 'face_fix', 'hand_fix' or 'wash'."""
    if mode == 'wash':
        return WASH_PROMPT
    return HAND_FIX_PROMPT if mode == 'hand_fix' else face_fix_prompt(snapshot)
