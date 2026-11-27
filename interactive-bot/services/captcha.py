"""Captcha helpers with a cached view of the image directory."""

import os

_IMAGE_NAMES: tuple[str, ...] | None = None


def image_choices(directory: str = "./assets/imgs") -> tuple[str, ...]:
    """Return image file names, scanning the directory only once."""
    global _IMAGE_NAMES
    if _IMAGE_NAMES is None:
        _IMAGE_NAMES = tuple(os.listdir(directory))
    return _IMAGE_NAMES
