from __future__ import annotations

from pathlib import Path


def test_standalone_tree_has_no_marketimmune_imports() -> None:
    roots = (Path("hindsight"), Path("tests"))
    package_name = "market" + "immune"
    forbidden = (f"from {package_name}", f"import {package_name}")
    offenders: list[Path] = []
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(pattern in text for pattern in forbidden):
                offenders.append(path)

    assert offenders == []
