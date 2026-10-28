"""Portrait/landscape picture frames shared by settings, the director and prompts."""

ORIENTATIONS = ('portrait', 'landscape')
ORIENTATION_NAMES = {'portrait': '竖版', 'landscape': '横版'}
# The user picks one portrait ratio; its landscape partner is the same ratio turned sideways.
PORTRAIT_RATIOS = ('3:4', '2:3', '9:16')
DEFAULT_PORTRAIT_RATIO = '3:4'


def ratio_for(orientation: str, portrait_ratio: str) -> str:
    if orientation not in ORIENTATIONS or portrait_ratio not in PORTRAIT_RATIOS:
        raise ValueError('invalid orientation or portrait ratio')
    if orientation == 'portrait':
        return portrait_ratio
    width, height = portrait_ratio.split(':')
    return f'{height}:{width}'


def orientation_of(aspect_ratio: str) -> str:
    width, height = (int(value) for value in aspect_ratio.split(':'))
    return 'landscape' if width > height else 'portrait'
