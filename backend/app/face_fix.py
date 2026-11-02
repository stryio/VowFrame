"""AI face fix (V7.1; the whole picture since V7.2): make both faces in a finished picture look like the couple,
keeping hair, makeup and retouching (docs/prompts.md section 9).

FaceFusion's swap re-renders the face from the uploaded photo, and the bare-faced photo washes the makeup out.
Here the image model makes the likeness edit: one edit of the whole picture with both looks (V7.4; the uploaded
photos before, whose bare skin and old hair the edit copied onto the faces). V7.9: the bride's reference is her
made-up photo before the hair edit when her look has one, with its own prompt that takes no hair from it.

V7.1 edited a crop around each face and laid only the face back. The laid-back face did not belong to the head
around it (a different expression and light inside the same outline), and looked like a pasted meme face, so the
whole edit is kept as it comes back.
"""

import json

from .prompts import FACE_FIX_MAKEUP_PROMPT, FACE_FIX_PROMPT


def face_fix_snapshot(model: str, aspect_ratio: str, bride_reference: str = 'look') -> str:
    """Model record stored on face-fix tasks: the edit keeps the picture's frame. bride_reference (V7.9): 'makeup'
    when the bride's reference is her made-up photo before the hair edit, 'look' when it is her look (older tasks
    have no key and used the look)."""
    return json.dumps({'generator': 'face_fix', 'model': model, 'aspect_ratio': aspect_ratio,
                       'bride_reference': bride_reference}, ensure_ascii=False)


def face_fix_prompt(snapshot: dict) -> str:
    """The edit prompt for the references the task recorded."""
    return FACE_FIX_MAKEUP_PROMPT if snapshot.get('bride_reference') == 'makeup' else FACE_FIX_PROMPT
