"""Shared helpers for temporary image file paths used by ViewPilot."""

import os
import tempfile


def sanitize_token(value):
    """Convert text to a filesystem-safe token."""
    text = str(value) if value is not None else "item"
    safe = []
    for ch in text:
        if ch.isalnum() or ch in ("_", "-"):
            safe.append(ch)
        else:
            safe.append("_")
    token = "".join(safe).strip("_")
    return token or "item"


def make_temp_png_path(prefix, name):
    """Build a deterministic temp PNG path from a prefix and name."""
    return os.path.join(tempfile.gettempdir(), f"{prefix}{sanitize_token(name)}.png")
