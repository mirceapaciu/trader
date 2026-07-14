from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.product_components.market_data.models import (
    ContextSourceStatus,
    FetchRun,
    Instrument,
    InstrumentFundamentals,
    MarketBar,
    MarketContextSnapshot,
    MarketDataProvider,
    MarketQuote,
    ProviderSymbol,
    QuoteDataType,
)
from src.product_components.shared.adapters import SharedInstrumentRecord

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class InstrumentRegistry(Protocol):
    def list_active_instruments(self) -> list[SharedInstrumentRecord]:
        """Return active shared instrument registry rows."""


class ApiUsageWriter(Protocol):
    def record_usage(
        self,
        *,
        provider: str,
        endpoint: str,
        called_at: datetime,
        tokens_used: int | None = None,
        cost_estimate: float | None = None,
    ) -> None:
        """Record shared API usage through the shared-owned contract."""


class PostgresMarketDataStorageAdapter:
    """PostgreSQL persistence adapter for the MarketData cache."""

    def __init__(
        self,
        *,
        dsn: str,
        market_data_schema: str,
        instrument_registry: InstrumentRegistry,
        api_usage_writer: ApiUsageWriter,
    ) -> None:
        self._dsn = dsn
        self._market_data_schema = _safe_identifier(market_data_schema)
        self._instrument_registry = instrument_registry
        self._api_usage_writer = api_usage_writer

    def load_active_instruments(self) -> list[Instrument]:
        return [
            Instrument(
                ticker=str(row.ticker).strip().upper(),
                exchange_code=str(row.exchange_code).strip().upper(),
            )
            for row in self._instrument_registry.list_active_instruments()
        ]

    def load_provider_symbols(
        self,
        *,
        ticker: str,
        exchange_code: str,
        provider: MarketDataProvider | None = None,
    ) -> list[ProviderSymbol]:
        params: list[Any] = [ticker.strip().upper(), exchange_code.strip().upper()]
        provider_filter = ""
        if provider is not None:
            provider_filter = "AND provider = %s "
            params.append(provider.value)
        sql = (
            f"SELECT ticker, exchange_code, provider, provider_symbol, currency, asset_class, "
            f"provider_metadata, is_verified, is_enabled "
            f"FROM {self._market_data_schema}.t_market_provider_symbols "
            f"WHERE ticker = %s AND exchange_code = %s "
            f"AND is_enabled = TRUE AND is_verified = TRUE "
            f"{provider_filter}"
            f"ORDER BY provider"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [_provider_symbol(row) for row in rows]

    def upsert_provider_symbol(self, symbol: ProviderSymbol) -> None:
        sql = (
            f"INSERT INTO {self._market_data_schema}.t_market_provider_symbols "
            f"(ticker, exchange_code, provider, provider_symbol, currency, asset_class, "
            f"provider_metadata, is_verified, is_enabled, created_at, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW()) "
            f"ON CONFLICT (ticker, exchange_code, provider) DO UPDATE SET "
            f"provider_symbol = EXCLUDED.provider_symbol, "
            f"currency = EXCLUDED.currency, "
            f"asset_class = EXCLUDED.asset_class, "
            f"provider_metadata = EXCLUDED.provider_metadata, "
            f"is_verified = EXCLUDED.is_verified, "
            f"is_enabled = EXCLUDED.is_enabled, "
            f"updated_at = NOW()"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    symbol.ticker,
                    symbol.exchange_code,
                    symbol.provider.value,
                    symbol.provider_symbol,
                    symbol.currency,
                    symbol.asset_class,
                    Json(symbol.provider_metadata),
                    symbol.is_verified,
                    symbol.is_enabled,
                ),
            )
            conn.commit()

    def upsert_quote(self, quote: MarketQuote) -> None:
        sql = (
            f"INSERT INTO {self._market_data_schema}.t_market_quotes "
            f"(ticker, exchange_code, provider, data_type, currency, bid_price, ask_price, "
            f"last_price, previous_close, volume, provider_timestamp, fetched_at, provider_metadata) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (ticker, exchange_code, provider, data_type) DO UPDATE SET "
            f"currency = EXCLUDED.currency, "
            f"bid_price = EXCLUDED.bid_price, "
            f"ask_price = EXCLUDED.ask_price, "
            f"last_price = EXCLUDED.last_price, "
            f"previous_close = EXCLUDED.previous_close, "
            f"volume = EXCLUDED.volume, "
            f"provider_timestamp = EXCLUDED.provider_timestamp, "
            f"fetched_at = EXCLUDED.fetched_at, "
            f"provider_metadata = EXCLUDED.provider_metadata"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    quote.ticker,
                    quote.exchange_code,
                    quote.provider.value,
                    quote.data_type.value,
                    quote.currency,
                    quote.bid_price,
                    quote.ask_price,
                    quote.last_price,
                    quote.previous_close,
                    quote.volume,
                    _to_utc(quote.provider_timestamp) if quote.provider_timestamp else None,
                    _to_utc(quote.fetched_at),
                    Json(quote.provider_metadata),
                ),
            )
            conn.commit()

    def upsert_bars(self, bars: list[MarketBar]) -> None:
        if not bars:
            return
        sql = (
            f"INSERT INTO {self._market_data_schema}.t_market_bars "
            f"(ticker, exchange_code, provider, bar_interval, bar_start_at, currency, open_price, "
            f"high_price, low_price, close_price, volume, adjusted, fetched_at, provider_metadata) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (ticker, exchange_code, provider, bar_interval, bar_start_at, adjusted) DO UPDATE SET "
            f"currency = EXCLUDED.currency, "
            f"open_price = EXCLUDED.open_price, "
            f"high_price = EXCLUDED.high_price, "
            f"low_price = EXCLUDED.low_price, "
            f"close_price = EXCLUDED.close_price, "
            f"volume = EXCLUDED.volume, "
            f"fetched_at = EXCLUDED.fetched_at, "
            f"provider_metadata = EXCLUDED.provider_metadata"
        )
        with self._connect() as conn, conn.cursor() as cur:
            for bar in bars:
                cur.execute(
                    sql,
                    (
                        bar.ticker,
                        bar.exchange_code,
                        bar.provider.value,
                        bar.bar_interval,
                        _to_utc(bar.bar_start_at),
                        bar.currency,
                        bar.open_price,
                        bar.high_price,
                        bar.low_price,
                        bar.close_price,
                        bar.volume,
                        bar.adjusted,
                        _to_utc(bar.fetched_at),
                        Json(bar.provider_metadata),
                    ),
                )
            conn.commit()

    def load_latest_quote(
        self,
        *,
        ticker: str,
        exchange_code: str,
        provider: MarketDataProvider | None = None,
    ) -> MarketQuote | None:
        params: list[Any] = [ticker.strip().upper(), exchange_code.strip().upper()]
        provider_filter = ""
        if provider is not None:
            provider_filter = "AND provider = %s "
            params.append(provider.value)
        sql = (
            f"SELECT ticker, exchange_code, provider, data_type, currency, bid_price, ask_price, "
            f"last_price, previous_close, volume, provider_timestamp, fetched_at, provider_metadata "
            f"FROM {self._market_data_schema}.t_market_quotes "
            f"WHERE ticker = %s AND exchange_code = %s "
            f"{provider_filter}"
            f"ORDER BY fetched_at DESC LIMIT 1"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
        return _market_quote(row) if row else None

    def load_bars(
        self,
        *,
        ticker: str,
        exchange_code: str,
        bar_interval: str,
        limit: int,
    ) -> list[MarketBar]:
        sql = (
            f"SELECT ticker, exchange_code, provider, bar_interval, bar_start_at, currency, "
            f"open_price, high_price, low_price, close_price, volume, adjusted, fetched_at, provider_metadata "
            f"FROM {self._market_data_schema}.t_market_bars "
            f"WHERE ticker = %s AND exchange_code = %s AND bar_interval = %s "
            f"ORDER BY bar_start_at DESC LIMIT %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                sql,
                (
                    ticker.strip().upper(),
                    exchange_code.strip().upper(),
                    bar_interval,
                    limit,
                ),
            )
            rows = cur.fetchall()
        return sorted((_market_bar(row) for row in rows), key=lambda bar: bar.bar_start_at)

    def load_bars_in_range(
        self,
        *,
        ticker: str,
        exchange_code: str,
        bar_interval: str,
        start: datetime,
        end: datetime,
        adjusted: bool = False,
    ) -> list[MarketBar]:
        sql = (
            f"SELECT ticker, exchange_code, provider, bar_interval, bar_start_at, currency, "
            f"open_price, high_price, low_price, close_price, volume, adjusted, fetched_at, provider_metadata "
            f"FROM {self._market_data_schema}.t_market_bars "
            f"WHERE ticker = %s AND exchange_code = %s AND bar_interval = %s AND adjusted = %s "
            f"AND bar_start_at >= %s AND bar_start_at <= %s "
            f"ORDER BY bar_start_at ASC"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                sql,
                (
                    ticker.strip().upper(),
                    exchange_code.strip().upper(),
                    bar_interval,
                    adjusted,
                    _to_utc(start),
                    _to_utc(end),
                ),
            )
            rows = cur.fetchall()
        return sorted((_market_bar(row) for row in rows), key=lambda bar: bar.bar_start_at)

    def load_bar_coverage(
        self,
        *,
        ticker: str,
        exchange_code: str,
        provider: MarketDataProvider,
        bar_interval: str,
    ) -> tuple[datetime, datetime] | None:
        sql = (
            f"SELECT covered_start, covered_end "
            f"FROM {self._market_data_schema}.t_market_bar_coverage "
            f"WHERE ticker = %s AND exchange_code = %s AND provider = %s AND bar_interval = %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                sql,
                (
                    ticker.strip().upper(),
                    exchange_code.strip().upper(),
                    provider.value,
                    bar_interval,
                ),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return (row["covered_start"], row["covered_end"])

    def upsert_bar_coverage(
        self,
        *,
        ticker: str,
        exchange_code: str,
        provider: MarketDataProvider,
        bar_interval: str,
        covered_start: datetime,
        covered_end: datetime,
    ) -> None:
        sql = (
            f"INSERT INTO {self._market_data_schema}.t_market_bar_coverage "
            f"(ticker, exchange_code, provider, bar_interval, covered_start, covered_end, fetched_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, NOW()) "
            f"ON CONFLICT (ticker, exchange_code, provider, bar_interval) DO UPDATE SET "
            f"covered_start = LEAST(t_market_bar_coverage.covered_start, EXCLUDED.covered_start), "
            f"covered_end = GREATEST(t_market_bar_coverage.covered_end, EXCLUDED.covered_end), "
            f"fetched_at = NOW()"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    ticker.strip().upper(),
                    exchange_code.strip().upper(),
                    provider.value,
                    bar_interval,
                    _to_utc(covered_start),
                    _to_utc(covered_end),
                ),
            )
            conn.commit()

    def upsert_context_snapshot(self, snapshot: MarketContextSnapshot) -> None:
        sql = (
            f"INSERT INTO {self._market_data_schema}.t_market_context_snapshots "
            f"(ticker, exchange_code, as_of, source_status, current_price, previous_close, "
            f"return_1d, return_5d, return_20d, atr_20d, volatility_20d, volume_ratio_20d, "
            f"sma_20d, sma_50d, recent_high_20d, recent_low_20d, drawdown_from_high_20d, "
            f"quote_fetched_at, bars_fetched_at, created_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()) "
            f"ON CONFLICT (ticker, exchange_code) DO UPDATE SET "
            f"as_of = EXCLUDED.as_of, "
            f"source_status = EXCLUDED.source_status, "
            f"current_price = EXCLUDED.current_price, "
            f"previous_close = EXCLUDED.previous_close, "
            f"return_1d = EXCLUDED.return_1d, "
            f"return_5d = EXCLUDED.return_5d, "
            f"return_20d = EXCLUDED.return_20d, "
            f"atr_20d = EXCLUDED.atr_20d, "
            f"volatility_20d = EXCLUDED.volatility_20d, "
            f"volume_ratio_20d = EXCLUDED.volume_ratio_20d, "
            f"sma_20d = EXCLUDED.sma_20d, "
            f"sma_50d = EXCLUDED.sma_50d, "
            f"recent_high_20d = EXCLUDED.recent_high_20d, "
            f"recent_low_20d = EXCLUDED.recent_low_20d, "
            f"drawdown_from_high_20d = EXCLUDED.drawdown_from_high_20d, "
            f"quote_fetched_at = EXCLUDED.quote_fetched_at, "
            f"bars_fetched_at = EXCLUDED.bars_fetched_at, "
            f"created_at = NOW()"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    snapshot.ticker,
                    snapshot.exchange_code,
                    _to_utc(snapshot.as_of),
                    snapshot.source_status.value,
                    snapshot.current_price,
                    snapshot.previous_close,
                    snapshot.return_1d,
                    snapshot.return_5d,
                    snapshot.return_20d,
                    snapshot.atr_20d,
                    snapshot.volatility_20d,
                    snapshot.volume_ratio_20d,
                    snapshot.sma_20d,
                    snapshot.sma_50d,
                    snapshot.recent_high_20d,
                    snapshot.recent_low_20d,
                    snapshot.drawdown_from_high_20d,
                    _to_utc(snapshot.quote_fetched_at) if snapshot.quote_fetched_at else None,
                    _to_utc(snapshot.bars_fetched_at) if snapshot.bars_fetched_at else None,
                ),
            )
            conn.commit()

    def get_market_context(
        self,
        *,
        ticker: str,
        exchange_code: str,
    ) -> MarketContextSnapshot | None:
        sql = (
            f"SELECT ticker, exchange_code, as_of, source_status, current_price, previous_close, "
            f"return_1d, return_5d, return_20d, atr_20d, volatility_20d, volume_ratio_20d, "
            f"sma_20d, sma_50d, recent_high_20d, recent_low_20d, drawdown_from_high_20d, "
            f"quote_fetched_at, bars_fetched_at "
            f"FROM {self._market_data_schema}.t_market_context_snapshots "
            f"WHERE ticker = %s AND exchange_code = %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (ticker.strip().upper(), exchange_code.strip().upper()))
            row = cur.fetchone()
        return _context_snapshot(row) if row else None

    _FUNDAMENTALS_COLUMNS = (
        "ticker, exchange_code, provider, market_cap_usd, shares_outstanding, "
        "revenue_ttm_usd, next_earnings_date, payload_json, fetched_at, last_checked_at"
    )

    def load_latest_fundamentals(
        self, *, ticker: str, exchange_code: str
    ) -> InstrumentFundamentals | None:
        sql = (
            f"SELECT {self._FUNDAMENTALS_COLUMNS} "
            f"FROM {self._market_data_schema}.t_instrument_fundamentals "
            f"WHERE ticker = %s AND exchange_code = %s "
            f"ORDER BY fetched_at DESC LIMIT 1"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (ticker.strip().upper(), exchange_code.strip().upper()))
            row = cur.fetchone()
        return _instrument_fundamentals(row) if row else None

    def load_fundamentals_as_of(
        self, *, ticker: str, exchange_code: str, as_of: datetime
    ) -> InstrumentFundamentals | None:
        sql = (
            f"SELECT {self._FUNDAMENTALS_COLUMNS} "
            f"FROM {self._market_data_schema}.t_instrument_fundamentals "
            f"WHERE ticker = %s AND exchange_code = %s AND fetched_at <= %s "
            f"ORDER BY fetched_at DESC LIMIT 1"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                sql,
                (ticker.strip().upper(), exchange_code.strip().upper(), _to_utc(as_of)),
            )
            row = cur.fetchone()
        return _instrument_fundamentals(row) if row else None

    def save_fundamentals(self, snapshot: InstrumentFundamentals) -> None:
        """Append-only with change detection: identical values only touch last_checked_at."""
        latest = self.load_latest_fundamentals(
            ticker=snapshot.ticker, exchange_code=snapshot.exchange_code
        )
        if latest is not None and _fundamentals_values_equal(latest, snapshot):
            sql = (
                f"UPDATE {self._market_data_schema}.t_instrument_fundamentals "
                f"SET last_checked_at = %s "
                f"WHERE ticker = %s AND exchange_code = %s AND fetched_at = %s"
            )
            params = (
                _to_utc(snapshot.last_checked_at),
                latest.ticker,
                latest.exchange_code,
                _to_utc(latest.fetched_at),
            )
        else:
            sql = (
                f"INSERT INTO {self._market_data_schema}.t_instrument_fundamentals "
                f"({self._FUNDAMENTALS_COLUMNS}) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            )
            params = (
                snapshot.ticker.strip().upper(),
                snapshot.exchange_code.strip().upper(),
                snapshot.provider.value,
                snapshot.market_cap_usd,
                snapshot.shares_outstanding,
                snapshot.revenue_ttm_usd,
                snapshot.next_earnings_date,
                Json(snapshot.payload),
                _to_utc(snapshot.fetched_at),
                _to_utc(snapshot.last_checked_at),
            )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()

    def record_fetch_run(self, run: FetchRun) -> None:
        sql = (
            f"INSERT INTO {self._market_data_schema}.t_market_data_fetch_runs "
            f"(provider, operation, ticker, exchange_code, status, error_code, started_at, finished_at, fetched_count, details) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    run.provider.value,
                    run.operation,
                    run.ticker,
                    run.exchange_code,
                    run.status,
                    run.error_code,
                    _to_utc(run.started_at),
                    _to_utc(run.finished_at),
                    run.fetched_count,
                    Json(run.details),
                ),
            )
            conn.commit()

    def record_api_usage(self, *, provider: MarketDataProvider, endpoint: str, called_at: datetime) -> None:
        self._api_usage_writer.record_usage(
            provider=provider.value,
            endpoint=endpoint,
            called_at=called_at,
        )

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _instrument_fundamentals(row: dict[str, Any]) -> InstrumentFundamentals:
    return InstrumentFundamentals(
        ticker=str(row["ticker"]),
        exchange_code=str(row["exchange_code"]),
        provider=MarketDataProvider(str(row["provider"])),
        market_cap_usd=float(row["market_cap_usd"]) if row["market_cap_usd"] is not None else None,
        shares_outstanding=(
            float(row["shares_outstanding"]) if row["shares_outstanding"] is not None else None
        ),
        revenue_ttm_usd=float(row["revenue_ttm_usd"]) if row["revenue_ttm_usd"] is not None else None,
        next_earnings_date=row["next_earnings_date"],
        payload=dict(row["payload_json"] or {}),
        fetched_at=_to_utc(row["fetched_at"]),
        last_checked_at=_to_utc(row["last_checked_at"]),
    )


def _fundamentals_values_equal(a: InstrumentFundamentals, b: InstrumentFundamentals) -> bool:
    # Only the prompt-visible fields participate: a change in the raw payload
    # alone must not create a new point-in-time row (it would needlessly split
    # the validity window and perturb regeneration prompt selection).
    return (
        a.market_cap_usd == b.market_cap_usd
        and a.shares_outstanding == b.shares_outstanding
        and a.revenue_ttm_usd == b.revenue_ttm_usd
        and a.next_earnings_date == b.next_earnings_date
    )


def _provider_symbol(row: dict[str, Any]) -> ProviderSymbol:
    return ProviderSymbol(
        ticker=str(row["ticker"]),
        exchange_code=str(row["exchange_code"]),
        provider=MarketDataProvider(str(row["provider"])),
        provider_symbol=str(row["provider_symbol"]),
        currency=str(row["currency"]),
        asset_class=str(row["asset_class"]),
        provider_metadata=dict(row["provider_metadata"] or {}),
        is_verified=bool(row["is_verified"]),
        is_enabled=bool(row["is_enabled"]),
    )


def _market_quote(row: dict[str, Any]) -> MarketQuote:
    return MarketQuote(
        ticker=str(row["ticker"]),
        exchange_code=str(row["exchange_code"]),
        provider=MarketDataProvider(str(row["provider"])),
        data_type=QuoteDataType(str(row["data_type"])),
        currency=str(row["currency"]),
        bid_price=row["bid_price"],
        ask_price=row["ask_price"],
        last_price=row["last_price"],
        previous_close=row["previous_close"],
        volume=row["volume"],
        provider_timestamp=row["provider_timestamp"],
        fetched_at=row["fetched_at"],
        provider_metadata=dict(row["provider_metadata"] or {}),
    )


def _market_bar(row: dict[str, Any]) -> MarketBar:
    return MarketBar(
        ticker=str(row["ticker"]),
        exchange_code=str(row["exchange_code"]),
        provider=MarketDataProvider(str(row["provider"])),
        bar_interval=str(row["bar_interval"]),
        bar_start_at=row["bar_start_at"],
        currency=str(row["currency"]),
        open_price=float(row["open_price"]),
        high_price=float(row["high_price"]),
        low_price=float(row["low_price"]),
        close_price=float(row["close_price"]),
        volume=row["volume"],
        adjusted=bool(row["adjusted"]),
        fetched_at=row["fetched_at"],
        provider_metadata=dict(row["provider_metadata"] or {}),
    )


def _context_snapshot(row: dict[str, Any]) -> MarketContextSnapshot:
    return MarketContextSnapshot(
        ticker=str(row["ticker"]),
        exchange_code=str(row["exchange_code"]),
        as_of=row["as_of"],
        source_status=ContextSourceStatus(str(row["source_status"])),
        current_price=row["current_price"],
        previous_close=row["previous_close"],
        return_1d=row["return_1d"],
        return_5d=row["return_5d"],
        return_20d=row["return_20d"],
        atr_20d=row["atr_20d"],
        volatility_20d=row["volatility_20d"],
        volume_ratio_20d=row["volume_ratio_20d"],
        sma_20d=row["sma_20d"],
        sma_50d=row["sma_50d"],
        recent_high_20d=row["recent_high_20d"],
        recent_low_20d=row["recent_low_20d"],
        drawdown_from_high_20d=row["drawdown_from_high_20d"],
        quote_fetched_at=row["quote_fetched_at"],
        bars_fetched_at=row["bars_fetched_at"],
    )
