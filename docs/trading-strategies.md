# Trading Strategies - Current Runtime Contract

## Overview

This document describes the trading behavior implemented by the news-based trading bot today. It is a current-behavior reference, not an aspirational strategy sketch.

The system targets multi-day, news-confirmed swing theses for US equities in the active watchlist. It does not attempt to win the first seconds or minutes after breaking news. ThesisBuilder waits for enough accepted evidence to form a coherent thesis, validates that the move is still actionable, creates a thesis card, and TradeExecutor converts that card into an ATR-based bracket order.

Authoritative component details live in:
- `docs/design/product_components/thesis-builder/behavior.md`
- `docs/design/product_components/trade-executor/behavior.md`
- `docs/design/shared/product-constraint.md`

## Implemented Strategy Status

| Strategy | Runtime status | Notes |
| --- | --- | --- |
| `event_driven` | Implemented | Supported in the v1 LLM strategy scope and eligible for executable thesis cards when evidence, market context, freshness, and already-priced gates pass. |
| `sentiment_momentum` | Implemented | Supported in the v1 LLM strategy scope and eligible for executable thesis cards under the same card-creation gates. |
| `sector_rotation` | Label-only / roadmap | The LLM may label an article as this best fit, but deterministic peer/sector generation and execution policy are not implemented. |
| `contrarian_reversal` | Label-only / roadmap | The LLM may label an article as this best fit, but the required market-context reversal checks and tighter risk policy are not implemented. |
| `trend_follow` | Label-only / roadmap | The behavior spec allows future longer-horizon trend cards, but v1 prompt scope does not create executable trend-follow cards. |

The v1 prompt explicitly scopes executable strategy reasoning to `event_driven` and `sentiment_momentum`. Unsupported strategies can still be labeled for audit so the system can learn where future strategy work is needed.

## Signal Generation

ThesisBuilder consumes accepted news events and analyzes each eligible article/instrument pair. A per-article LLM analysis must produce structured fields such as ticker, exchange code, sentiment, relevance, urgency, suggested action, candidate strategy, direction, confidence, and reasoning.

Eligible analyses are aggregated into rolling evidence windows keyed by instrument, strategy, and direction. A window can create a card only when it satisfies the shared product constraint and ThesisBuilder's evidence rules:
- enough accepted evidence exists for one coherent instrument, direction, strategy, and time horizon;
- evidence freshness is within the configured card freshness limit;
- conflicting high-confidence evidence does not invalidate the candidate;
- required market context is available and usable;
- the already-priced gate does not reject the candidate.

For `event_driven` and `sentiment_momentum`, ThesisBuilder rejects candidates when the direction-aligned one-day return or ATR-scaled move shows the trade may already be priced. Missing or stale market context for those strategies fails closed with `market_context_unavailable`.

## Trade Geometry

The default executable horizon is `swing_1d_5d`. TradeExecutor maps the card horizon to a maximum holding window and manages time exits from that mapping.

TradeExecutor does not use fixed +3% / -1.5% intraday exits. It builds levels from a fresh execution quote and MarketData's cached `atr_20d`:

| Direction | Stop | Take profit |
| --- | --- | --- |
| `buy` | `entry - ATR_STOP_MULT * atr_20d` | `entry + TAKE_PROFIT_R * (entry - stop)` |
| `sell` | `entry + ATR_STOP_MULT * atr_20d` | `entry - TAKE_PROFIT_R * (stop - entry)` |

The current defaults are:
- `ATR_STOP_MULT=1.5`
- `TAKE_PROFIT_R=2.0`

Orders are submitted as smart marketable-limit bracket orders: parent entry, protective stop, and take-profit child. If neither bracket exit fills before the horizon elapses, TradeExecutor flattens the position with a time exit.

## Position Sizing

Sizing is risk-box based, not confidence-band based. ThesisBuilder writes an initial risk box with `max_loss_usd`, stop condition, and invalidation condition. TradeExecutor converts the dollar risk budget into quantity:

```text
qty = floor(risk_box.max_loss_usd / abs(entry - stop))
```

The quantity is then clamped by maximum position size and remaining portfolio headroom. A card can still be rejected if the resulting quantity is below one share or if portfolio guardrails fail.

The card confidence is still persisted and admission-gated by configuration, but it is not the sizing formula. Confidence calibration is tracked separately in issue `260709-04`; until that work proves discrimination, confidence should not be treated as a calibrated sizing signal.

## Admission And Risk Gates

ThesisBuilder emits a signal only after a valid thesis card is persisted, initial shared review is approved by system policy, and the card is fresh.

TradeExecutor then admits or rejects each card through deterministic gates:
- `direction = hold`
- card expired
- confidence below `TRADE_EXECUTOR_MIN_CONFIDENCE`
- instrument not in the active watchlist
- existing live or working position for the instrument
- duplicate card
- shared review missing or not approved
- unmapped time horizon

If admission passes, TradeExecutor still requires:
- fresh execution quote;
- usable `atr_20d`;
- size of at least one share;
- portfolio exposure, open-position, sector-exposure, daily-trade, and daily-loss limits.

Every deliberate drop is persisted with a machine-readable reason code.

## Roadmap Strategies

The following ideas are not current executable behavior:
- contrarian reversal based on five articles in two hours;
- sector peer expansion through `SECTOR_PEERS`;
- recency-weighted sentiment aggregation thresholds such as `+0.6` / `-0.6`;
- confidence-band position sizing such as $1,000 / $500 / no trade;
- fixed intraday exits such as +3%, -1.5%, or four-hour time stops.

To promote a roadmap strategy into executable behavior, the implementation needs a deterministic policy in ThesisBuilder, market-context requirements, risk-box rules, TradeExecutor compatibility, and backtest evidence recorded in a verification report.

## Performance Tracking

Backtests and live audit should evaluate the implemented swing system with metrics such as:
- closed-trade P&L, win rate, average win/loss, and profit factor;
- drawdown and daily P&L;
- card counts by strategy, direction, rejection reason, and evidence-window outcome;
- bracket exit reason: stop, take profit, or time;
- confidence calibration by outcome bucket.

No current runtime target promises 3-5 intraday momentum trades per day, 1-4 hour average holds, or a fixed 55-60% win rate. Performance targets should be set from verified backtest and paper-trading results for the swing regime.

## Backtesting

Before enabling or changing a strategy with real money:

1. Replay the pipeline over historical news and price data.
2. Verify data integrity, funnel counts, and trade attribution.
3. Compare closed-trade outcomes, rejection reasons, and opportunity cost.
4. Paper trade before live trading.
5. Record results in `docs/verification-runs/`.

Backtests must account for historical LLM variability, slippage approximations, survivorship bias, and the difference between regenerated and live-streamed article timing.

## Disclaimer

These strategies are for educational and personal use. Automated trading carries significant risk of loss. Past performance, including backtests, does not guarantee future results.
