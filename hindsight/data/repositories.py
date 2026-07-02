"""Binance parquet repositories that are not present in the v1 simulator."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq


@dataclass(frozen=True, slots=True)
class KlineRecord:
    """One 1-minute OHLCV bar from a Binance USD-M parquet file."""

    event_id: str
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class DepthLevel:
    """One aggregated percent-from-mid depth level."""

    percentage: float
    depth: float
    notional: float


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    """All percent-from-mid depth levels recorded at one timestamp."""

    timestamp: datetime
    levels: tuple[DepthLevel, ...]


@dataclass(frozen=True, slots=True)
class AggTradeRecord:
    """One Binance aggregate trade row."""

    event_id: str
    timestamp: datetime
    symbol: str
    aggregate_trade_id: int
    price: float
    quantity: float
    first_trade_id: int
    last_trade_id: int
    is_buyer_maker: bool
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class BookTickerRecord:
    """One Binance top-of-book ticker row."""

    event_id: str
    timestamp: datetime
    symbol: str
    update_id: int
    bid_price: float
    bid_quantity: float
    ask_price: float
    ask_quantity: float
    raw: dict[str, object]


class KlineRepository:
    """Loads 1-minute kline rows for a symbol from Binance parquet files."""

    def __init__(self, lake_root: Path):
        self.lake_root = Path(lake_root)

    def directory_for(self, symbol: str, interval: str = "1m") -> Path:
        return self.lake_root / "klines" / symbol / interval

    def available_dates(self, symbol: str) -> list[str]:
        symbol_directory = self.directory_for(symbol)
        if not symbol_directory.exists():
            return []
        return sorted(_date_from_name(file) for file in symbol_directory.glob("*.parquet"))

    def load(self, symbol: str, date: str | None, limit: int) -> list[KlineRecord]:
        files = _chosen_files(self.directory_for(symbol), date)
        rows = _limited_rows(files, limit)
        records: list[KlineRecord] = []
        for row in rows:
            records.append(
                KlineRecord(
                    event_id=str(row.get("event_id", "")),
                    timestamp=_parse_timestamp(row["timestamp"]),
                    symbol=symbol.upper(),
                    open=_as_float(row["open_price"]),
                    high=_as_float(row["high_price"]),
                    low=_as_float(row["low_price"]),
                    close=_as_float(row["close_price"]),
                    volume=_as_float(row["volume"]),
                    trade_count=_as_int(row.get("trade_count", 0)),
                    raw=row,
                )
            )
        records.sort(key=lambda record: record.timestamp)
        return records


class DepthRepository:
    """Loads aggregated depth snapshots for a symbol from Binance parquet files."""

    def __init__(self, lake_root: Path):
        self.lake_root = Path(lake_root)

    def directory_for(self, symbol: str) -> Path:
        return self.lake_root / "bookDepth" / symbol

    def available_dates(self, symbol: str) -> list[str]:
        symbol_directory = self.directory_for(symbol)
        if not symbol_directory.exists():
            return []
        return sorted(_date_from_name(file) for file in symbol_directory.glob("*.parquet"))

    def load(self, symbol: str, date: str) -> list[DepthSnapshot]:
        path = self.directory_for(symbol) / f"{symbol}-bookDepth-{date}.parquet"
        if not path.exists():
            return []
        rows = _limited_rows([path], limit=10_000_000)
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            grouped.setdefault(str(row["timestamp"]), []).append(row)

        snapshots: list[DepthSnapshot] = []
        for timestamp, levels in grouped.items():
            levels.sort(key=lambda level: _as_float(level["percentage"]))
            snapshots.append(
                DepthSnapshot(
                    timestamp=_parse_timestamp(timestamp),
                    levels=tuple(
                        DepthLevel(
                            percentage=_as_float(level["percentage"]),
                            depth=_as_float(level["depth"]),
                            notional=_as_float(level["notional"]),
                        )
                        for level in levels
                    ),
                )
            )
        snapshots.sort(key=lambda snapshot: snapshot.timestamp)
        return snapshots


class AggTradeRepository:
    """Loads aggregate trades for a symbol from Binance parquet files."""

    def __init__(self, lake_root: Path):
        self.lake_root = Path(lake_root)

    def directory_for(self, symbol: str) -> Path:
        return self.lake_root / "aggTrades" / symbol

    def available_dates(self, symbol: str) -> list[str]:
        symbol_directory = self.directory_for(symbol)
        # Fallback reason: M0 explicitly supports Binance lakes without aggTrades.
        # When the optional directory is absent, the stream is empty and the report
        # still records the data that did exist instead of inventing trades.
        if not symbol_directory.exists():
            return []
        return sorted(_date_from_name(file) for file in symbol_directory.glob("*.parquet"))

    def load(self, symbol: str, date: str | None, limit: int) -> list[AggTradeRecord]:
        files = _chosen_files(self.directory_for(symbol), date)
        rows = _limited_rows(files, limit)
        records: list[AggTradeRecord] = []
        for row in rows:
            records.append(
                AggTradeRecord(
                    event_id=str(row.get("event_id", "")),
                    timestamp=_parse_timestamp(row["timestamp"]),
                    symbol=symbol.upper(),
                    aggregate_trade_id=_as_int(row["aggregate_trade_id"]),
                    price=_as_float(row["price"]),
                    quantity=_as_float(row["quantity"]),
                    first_trade_id=_as_int(row["first_trade_id"]),
                    last_trade_id=_as_int(row["last_trade_id"]),
                    is_buyer_maker=_as_bool(row["is_buyer_maker"]),
                    raw=row,
                )
            )
        records.sort(key=lambda record: record.timestamp)
        return records


class BookTickerRepository:
    """Loads top-of-book ticker rows for a symbol from Binance parquet files."""

    def __init__(self, lake_root: Path):
        self.lake_root = Path(lake_root)

    def directory_for(self, symbol: str) -> Path:
        return self.lake_root / "bookTicker" / symbol

    def available_dates(self, symbol: str) -> list[str]:
        symbol_directory = self.directory_for(symbol)
        # Fallback reason: M0 explicitly supports Binance lakes without bookTicker.
        # When the optional directory is absent, the stream is empty and later M1
        # can flag top-of-book-missing behavior without fabricating L1 quotes.
        if not symbol_directory.exists():
            return []
        return sorted(_date_from_name(file) for file in symbol_directory.glob("*.parquet"))

    def load(self, symbol: str, date: str | None, limit: int) -> list[BookTickerRecord]:
        files = _chosen_files(self.directory_for(symbol), date)
        rows = _limited_rows(files, limit)
        records: list[BookTickerRecord] = []
        for row in rows:
            records.append(
                BookTickerRecord(
                    event_id=str(row.get("event_id", "")),
                    timestamp=_parse_timestamp(row["timestamp"]),
                    symbol=symbol.upper(),
                    update_id=_as_int(row["update_id"]),
                    bid_price=_as_float(row["bid_price"]),
                    bid_quantity=_as_float(row["bid_quantity"]),
                    ask_price=_as_float(row["ask_price"]),
                    ask_quantity=_as_float(row["ask_quantity"]),
                    raw=row,
                )
            )
        records.sort(key=lambda record: record.timestamp)
        return records


def _chosen_files(directory: Path, date: str | None) -> list[Path]:
    # Fallback reason: the M0 plan says absent optional Binance datasets should
    # return an empty stream. This is a filesystem dependency and can be missing
    # in the current on-disk lake.
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.parquet"))
    # Same required fallback as above: an existing but empty partition has no rows.
    if not files:
        return []
    if date is None:
        return [files[0]]
    return [file for file in files if date in file.name]


def _limited_rows(files: Iterable[Path], limit: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in files:
        table = pq.read_table(path)  # type: ignore[no-untyped-call]
        rows.extend(dict(row) for row in table.to_pylist())
        if len(rows) >= limit:
            return rows[:limit]
    return rows


def _parse_timestamp(value: object) -> datetime:
    # Boundary reason: parquet writers in this repo produce ISO strings, Python
    # datetimes, or millisecond epochs. Accept those explicit encodings and let
    # invalid values fail instead of substituting a timestamp.
    if isinstance(value, datetime):
        return value
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, float):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _as_int(value: object) -> int:
    return int(str(value))


def _as_float(value: object) -> float:
    return float(str(value))


def _as_bool(value: object) -> bool:
    # Boundary reason: parquet may deserialize booleans as bools or strings.
    # Accept those explicit encodings; anything else is invalid market data.
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise ValueError(f"expected boolean parquet value, got {value!r}")


def _date_from_name(path: Path) -> str:
    return "-".join(path.stem.rsplit("-", 3)[-3:])
