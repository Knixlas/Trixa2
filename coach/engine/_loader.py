"""Intern hjälpmodul: laddar YAML-filer från ../data/ med cache.

Anropas av övriga engine-moduler. Stötta override-sökväg för tester.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


# Default-katalog: ../data/ relativt denna fil.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _resolve_path(filename: str, data_dir: Path | str | None = None) -> Path:
    base = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    return base / filename


@lru_cache(maxsize=32)
def _load_cached(path_str: str) -> dict[str, Any]:
    """Faktisk laddning. Cachas på den absoluta path-strängen."""
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"YAML-fil saknas: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_yaml(filename: str, data_dir: Path | str | None = None) -> dict[str, Any]:
    """Ladda en namngiven YAML-fil från data-katalogen.

    Args:
        filename: t.ex. "phases.yaml".
        data_dir: valfri override av data-katalog (för tester).

    Returns:
        Dict från YAML-rotnoden.
    """
    return _load_cached(str(_resolve_path(filename, data_dir).resolve()))


def clear_cache() -> None:
    """Töm cachen (användbart i tester efter att YAML uppdaterats)."""
    _load_cached.cache_clear()
