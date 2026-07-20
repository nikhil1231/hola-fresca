"""Helpers for building media URLs from stored source image paths.

Recipe/ingredient image paths are stored relative (e.g. ``/image/foo.jpg``).
HelloFresh serves them from a Cloudinary-style CDN that accepts an inline
transformation segment, so the frontend can request whatever size it needs:

    https://img.hellofresh.com/<transform>/hellofresh_s3<image_path>
"""
from __future__ import annotations

_CDN_HOST = "https://img.hellofresh.com"
_BUCKET = "hellofresh_s3"


def image_url(image_path: str | None, width: int = 500) -> str | None:
    """Return an absolute, width-optimised CDN URL for ``image_path``.

    Returns ``None`` when there is no image path so callers can fall back to a
    placeholder.
    """
    if not image_path:
        return None
    transform = f"f_auto,fl_lossy,q_auto,w_{width}"
    return f"{_CDN_HOST}/{transform}/{_BUCKET}{image_path}"
