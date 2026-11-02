"""AI face fix (V7.1; the whole picture since V7.2): make both faces in a finished picture look like the couple,
keeping hair, makeup and retouching (docs/prompts.md section 9).

FaceFusion's swap re-renders the face from the uploaded photo, and the bare-faced photo washes the makeup out.
Here the image model makes the likeness edit: one edit of the whole picture with both looks (V7.4; the uploaded
photos before, whose bare skin and old hair the edit copied onto the faces). V7.9–V7.13 used the bride's made-up
photo before the hair edit, and the edit copied its old hair even though the prompt said not to; V7.14 is back to her
look. Tasks recorded as 'makeup' keep their prompt.
V7.13 split mode edits only the bride, using the picture and her reference; a local FaceFusion task then swaps
only the groom. Manual face fixes and the explicit 'ai' mode continue editing both people.

V7.1 edited a crop around each face and laid only the face back. The laid-back face did not belong to the head
around it (a different expression and light inside the same outline), and looked like a pasted meme face, so the
whole edit is kept as it comes back.
"""

import json

from .prompts import FACE_FIX_BRIDE_MAKEUP_PROMPT, FACE_FIX_BRIDE_PROMPT, FACE_FIX_MAKEUP_PROMPT, FACE_FIX_PROMPT


def face_fix_snapshot(model: str, aspect_ratio: str, *, bride_only: bool = False) -> str:
    """Model record stored on face-fix tasks: the edit keeps the picture's frame. bride_reference: 'look' (V7.14);
    tasks from V7.9–V7.13 may say 'makeup', and tasks older than V7.9 have no key and used the look. bride_only
    (V7.13): the edit does only the bride, and FaceFusion swaps the groom afterwards ('roles' ['bride'], 'then'
    'groom_swap')."""
    snapshot = {'generator': 'face_fix', 'model': model, 'aspect_ratio': aspect_ratio, 'bride_reference': 'look'}
    if bride_only:
        snapshot.update(roles=['bride'], then='groom_swap')
    return json.dumps(snapshot, ensure_ascii=False)


def face_fix_prompt(snapshot: dict) -> str:
    """The edit prompt for the references and the people the task recorded."""
    makeup = snapshot.get('bride_reference') == 'makeup'
    if snapshot.get('roles') == ['bride']:
        return FACE_FIX_BRIDE_MAKEUP_PROMPT if makeup else FACE_FIX_BRIDE_PROMPT
    return FACE_FIX_MAKEUP_PROMPT if makeup else FACE_FIX_PROMPT
