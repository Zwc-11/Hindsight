"""Canonical-event adapter for the Hyperliquid parquet lake."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from hindsight.core.cursor import CursorEvent, EventCursor
from hindsight.core.events import CanonicalEvent, HyperliquidFillEvent, Side
from hindsight.data.lake import HyperliquidLakeLayout, read_parquet_records
from hindsight.ports.market_data import MarketDataPort


@dataclass(frozen=True, slots=True)
class HyperliquidMarkoutRecord:
    """One Hyperliquid gold markout row."""

    symbol: str
    timestamp: datetime
    price: float
    quantity: float
    side: Side
    maker_side: int
    trade_id: int | None
    markout_bps: dict[str, float]


class HyperliquidLakeAdapter(MarketDataPort):
    """Streams Hyperliquid lake rows as canonical events."""

    def __init__(self, lake_root: Path):
        self.layout = HyperliquidLakeLayout(Path(lake_root))

    def stream_events(
        self,
        *,
        symbol: str,
        date: str | None,
        limit: int,
    ) -> Iterator[CanonicalEvent]:
        if date is None:
            raise ValueError("Hyperliquid adapter requires an explicit date")
        if limit < 1:
            raise ValueError("limit must be positive")
        events = self._fill_events(symbol=symbol, date=date, limit=limit)
        for event in EventCursor(cast(list[CursorEvent], events)):
            yield cast(CanonicalEvent, event)

    def load_markouts(
        self,
        *,
        symbol: str,
        date: str,
        limit: int,
    ) -> list[HyperliquidMarkoutRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        coin = _coin_from_symbol(symbol)
        event_symbol = _perp_symbol(symbol)
        rows = read_parquet_records(self.layout.gold_markout_path(coin, date))
        records: list[HyperliquidMarkoutRecord] = []
        for row in rows[:limit]:
            records.append(
                HyperliquidMarkoutRecord(
                    symbol=event_symbol,
                    timestamp=_timestamp_ms(_require(row, "ts_ms")),
                    price=_float(row, "px"),
                    quantity=_float(row, "sz"),
                    side=_side(row),
                    maker_side=_maker_side(row),
                    trade_id=_optional_int(row.get("tid")),
                    markout_bps=_markout_columns(row),
                )
            )
        return records

    def _fill_events(self, *, symbol: str, date: str, limit: int) -> list[HyperliquidFillEvent]:
        coin = _coin_from_symbol(symbol)
        event_symbol = _perp_symbol(symbol)
        rows = read_parquet_records(self.layout.silver_fills_path(coin, date))
        events: list[HyperliquidFillEvent] = []
        for sequence, row in enumerate(rows[:limit]):
            events.append(
                HyperliquidFillEvent(
                    symbol=event_symbol,
                    timestamp=_timestamp_ms(_require(row, "ts_ms")),
                    sequence=sequence,
                    trade_id=_optional_int(row.get("tid")),
                    price=_float(row, "px"),
                    quantity=_float(row, "sz"),
                    side=_side(row),
                    crossed=_optional_bool(row.get("crossed")),
                    maker_side=_maker_side(row),
                    fee=_optional_float(row.get("fee")),
                    fee_token=_optional_str(row.get("fee_token")),
                )
            )
        return events


def _coin_from_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not cleaned:
        raise ValueError("symbol cannot be empty")
    return cleaned.removesuffix("-PERP")


def _perp_symbol(symbol: str) -> str:
    coin = _coin_from_symbol(symbol)
    return f"{coin}-PERP"


def _require(row: dict[str, Any], key: str) -> object:
    if key not in row:
        raise ValueError(f"Hyperliquid row missing required column: {key}")
    return row[key]


def _timestamp_ms(value: object) -> datetime:
    return datetime.fromtimestamp(int(str(value)) / 1000, tz=UTC)


def _float(row: dict[str, Any], key: str) -> float:
    return float(str(_require(row, key)))


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(str(value))


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(str(value))


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise ValueError(f"expected Hyperliquid boolean value, got {value!r}")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _side(row: dict[str, Any]) -> Side:
    raw = str(_require(row, "side")).strip().upper()
    if raw == "B":
        return Side.BUY
    if raw == "A":
        return Side.SELL
    raise ValueError(f"unknown Hyperliquid side: {raw!r}")


def _maker_side(row: dict[str, Any]) -> int:
    value = int(str(_require(row, "maker_side")))
    if value not in {-1, 1}:
        raise ValueError("maker_side must be -1 or 1")
    return value


def _markout_columns(row: dict[str, Any]) -> dict[str, float]:
    markouts = {
        key.removeprefix("markout_bps_"): float(str(value))
        for key, value in row.items()
        if key.startswith("markout_bps_") and value is not None
    }
    if not markouts:
        raise ValueError("Hyperliquid gold row has no markout_bps_* columns")
    return markouts
