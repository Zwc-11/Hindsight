"""Equity curve serialization helpers."""

from __future__ import annotations

from pathlib import Path

from hindsight.core.types import EquityPoint


def equity_curve_json(curve: tuple[EquityPoint, ...]) -> list[dict[str, object]]:
    return [
        {
            "timestamp": point.timestamp.isoformat(),
            "equity": point.equity,
            "cash": point.cash,
            "position_quantity": point.position_quantity,
            "mark_price": point.mark_price,
        }
        for point in curve
    ]


def ascii_sparkline(values: list[float]) -> str:
    if not values:
        raise ValueError("sparkline requires at least one value")
    glyphs = " .:-=+*#%@"
    low = min(values)
    high = max(values)
    if high == low:
        return glyphs[0] * len(values)
    scale = (len(glyphs) - 1) / (high - low)
    return "".join(glyphs[int((value - low) * scale)] for value in values)


def write_curve_png(path: Path, curve: tuple[EquityPoint, ...]) -> bool:
    # Fallback reason: M1 explicitly says PNG output is optional and matplotlib
    # must not become a hard dependency. JSON/Markdown still carry the curve.
    try:
        from matplotlib import pyplot as plt
    except ImportError:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(8, 3))
    plt.plot(list(range(len(curve))), [point.equity for point in curve])
    plt.tight_layout()
    plt.savefig(path)
    plt.close()
    return True
