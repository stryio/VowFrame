"""Confined asset paths and durable, atomic image writes."""

import os
from pathlib import Path

from .config import Config
from .db import new_id


def image_format(content: bytes) -> tuple[str, str]:
    if content.startswith(bytes.fromhex('89504e470d0a1a0a')):
        return '.png', 'image/png'
    if content.startswith(bytes.fromhex('ffd8ff')):
        return '.jpg', 'image/jpeg'
    if content.startswith((b'GIF87a', b'GIF89a')):
        return '.gif', 'image/gif'
    if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        return '.webp', 'image/webp'
    if content.startswith(b'BM'):
        return '.bmp', 'image/bmp'
    if content.startswith((bytes.fromhex('49492a00'), bytes.fromhex('4d4d002a'))):
        return '.tiff', 'image/tiff'
    raise ValueError('无法识别图片格式，请上传 PNG、JPEG、GIF、WebP、BMP 或 TIFF 图片')


def asset_path(config: Config, stored_path: str) -> Path:
    root = config.data_dir.resolve()
    path = (root / stored_path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('图片路径无效')
    return path


def save_image(config: Config, project_id: str, asset_id: str, content: bytes, *, original: bool = False) -> str:
    extension, _ = image_format(content)
    relative = Path('projects') / project_id / ('originals' if original else 'images') / f'{asset_id}{extension}'
    destination = asset_path(config, str(relative))
    destination.parent.mkdir(parents=True, exist_ok=True)
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    temporary = config.temp_dir / f'{new_id()}.tmp'
    try:
        with temporary.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return relative.as_posix()
