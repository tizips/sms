#!/usr/bin/env python3
from pathlib import Path


_PARTS = (
    "core.py",
    "status.py",
    "mail.py",
    "sending.py",
    "pages.py",
    "http_handler.py",
)


def _load_parts() -> None:
    # Keep one module namespace while the implementation lives in smaller files.
    # Existing file-path imports and monkeypatches keep working.
    here = Path(__file__).resolve().parent
    namespace = globals()
    for name in _PARTS:
        part = here / name
        exec(compile(part.read_text(encoding="utf-8"), str(part), "exec"), namespace)


_load_parts()


if __name__ == "__main__":
    run()
