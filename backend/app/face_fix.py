"""AI face fix (V7.1; the whole picture since V7.2): make both faces in a finished picture look like the uploaded
photos, keeping the makeup and retouching the picture already has (docs/prompts.md section 9).

FaceFusion's swap re-renders the face from the uploaded photo, and the bare-faced photo washes the makeup out.
Here the image model makes the likeness edit: one edit of the whole picture with both uploaded photos.

V7.1 edited a crop around each face and laid only the face back. The laid-back face did not belong to the head
around it (a different expression and light inside the same outline), and looked like a pasted meme face, so the
whole edit is kept as it comes back.
"""

import json


def face_fix_snapshot(model: str, aspect_ratio: str) -> str:
    """Model record stored on face-fix tasks: the edit keeps the picture's frame."""
    return json.dumps({'generator': 'face_fix', 'model': model, 'aspect_ratio': aspect_ratio}, ensure_ascii=False)
