#!/usr/bin/env python3
"""Совместимый запуск из checkout; основной интерфейс — установленный harness."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from ai_team_harness.cli import entrypoint

if __name__ == "__main__":
    raise SystemExit(entrypoint())
