"""Bounded tail reads. Never raise. About 64 KB, so a huge file cannot fill memory."""

from pathlib import Path

TAIL_BYTES = 65536


def read_tail(path, max_bytes=TAIL_BYTES):
    """Last `max_bytes` of a file as text. Mid-line prefix after a seek is dropped."""
    path = Path(path)
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = 0 if size <= max_bytes else size - max_bytes
            handle.seek(start)
            data = handle.read(max_bytes)
        text = data.decode("utf-8", errors="replace")
        if start > 0:
            newline = text.find("\n")
            if newline >= 0:
                text = text[newline + 1:]
        return text
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    except Exception:
        return ""
