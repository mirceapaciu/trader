# Design Viability Review — 2026-07-09

Point-in-time assessment of whether the application design and its trading strategies are
viable, and which design errors currently prevent profitable trades. Sources: design docs
(`docs/design/overview.md`, `docs/one-pager.md`, component behavior specs,
`docs/trading-strategies.md`), the implementation as of commit `cf32c40`, and the two
backtest verification reports in `docs/verification-runs/`.

## Verdict

The **engineering design is solid**. The **trading design is not currently viable**: the
pipeline as configured produces almost zero executable trades, and the repo's own
verification runs prove it. This is not "the strategy loses money" — it is the stronger
claim that the system, end to end, structurally cannot yet put on enough trades to even
test whether an edge exists. Lifetime results across all backtests at review time:
**1 closed trade (−$88.06), everything else risk-blocked or never generated**.

## Major design errors (confirmed, currently blocking profitability)

### 1. The generation config and the execution config exclude each other

The evidence gate (`THESIS_CARD_REQUIRED_EVIDENCE_COUNT = 3` articles for the same
ticker/direction/strategy within the collection window) is only cleared by tickers with
very dense news coverage — in both backtest windows, essentially only MU. But MU trades at
~$1,200/share with an ATR20 of ~$93, and the executor sizes with
`qty = floor(max_loss / stop_distance)` clamped by `floor(MAX_POSITION_SIZE / entry)`
(`src/product_components/trade_executor/pipeline.py::size_position`). With
`MAX_POSITION_SIZE = $1,000` and `risk_max_loss_usd = $120` vs a 1.5×ATR stop of ~$139, MU
sizes to **0 shares twice over**: its price exceeds the entire position cap, and its
per-share risk exceeds the entire risk box.

Per `docs/verification-runs/260708-bt_2416cea0515f427681f879f1c363ec3d.md`: *"the
generation config concentrates all card yield on exactly the ticker the trading config can
never execute."* That report's recommendation #1 (raise caps, fractional shares, or a
generation-side tradeability filter) was **still unapplied** at review time
(`trade_executor/settings.py` still defaults `MAX_POSITION_SIZE=1000`). Until fixed, every
card the system can produce is dead on arrival, and LLM budget is spent generating
unsizeable cards.

### 2. The stated edge guardrail does not exist

The strategic posture (`docs/design/overview.md` §1.4) is coherent and honest: cede fast
clean news to latency-sensitive players, capture post-event drift on confirmed multi-day
theses. That posture only works if the system refuses already-spent moves — and the docs
themselves flag already-priced suppression as a **KNOWN GAP, not implemented**
(overview §1.4, thesis-builder behavior §5.1). Market context is passed to the LLM
(`thesis_builder/llm_client.py::_build_prompt`) but no prompt instruction and no
deterministic gate suppresses a card whose move already happened.

Combined with the confirmation delay (3 sources + polling + LLM analysis takes hours *by
design*), this yields the worst configuration: **systematically late entry with no check
on whether the price has already moved**. The one closed trade ever (CRM long, entered on
the news day, stopped out −8.66% after an adverse gap) is exactly this failure shape. This
is the single most important strategy-level fix.

### 3. The evidence gate selects the wrong universe for the stated edge

Requiring 3 independent articles structurally favors mega-cap, news-saturated names —
precisely the most efficiently priced, where post-news drift is weakest. The design's own
thesis says the edge lives in "less-liquid names and slower information diffusion," but
those names cannot produce 3 articles in any reasonable window. There is a built-in
tension between the confirmation rule and where the returns supposedly are. The
rolling-window fix (29fee61) and widening `THESIS_BUILDER_EVIDENCE_COLLECTION_MAX_MINUTES`
to 1000 improve yield (~11–15 cards / 2 weeks projected) but do not resolve the selection
bias.

### 4. The confidence gates filter nothing

ThesisBuilder and TradeExecutor both gate on `min_confidence = 0.6`, but across 704 LLM
analyses in the two verification runs, **zero** were rejected below it — gpt-4o-mini's
self-reported confidence clusters at 0.78–0.85. LLM self-reported confidence is
uncalibrated; here it is used both as a trade gate and (per the strategy doc) a
position-sizing input, and it carries no information as configured. Nothing in the system
measures whether confidence correlates with outcomes.

### 5. `docs/trading-strategies.md` is aspirational, not what runs

The doc describes exits of +3% TP / −1.5% SL / 4-hour time stop; the implementation uses a
1.5×ATR stop, 2R target, and a 5-day time exit — a completely different trade geometry.
Three of its five strategies (contrarian reversal, sector rotation, trend follow) exist
only as prompt enum labels — `llm_client.py` explicitly scopes v1 to `event_driven` and
`sentiment_momentum`. The recency-weighted sentiment aggregation, the
5-articles-in-2-hours contrarian trigger, and the conflict-resolution matrix are not
implemented. Anyone evaluating "the strategies" from that doc is evaluating a fiction; it
should be marked stale or rewritten to match the ATR-bracket / evidence-window reality.

### 6. The validation loop cannot validate

Backtest regeneration runs get a 500k token budget that covers ~40–55% of a two-week
window, then report `status=completed` anyway (issue 260708-01). Two consecutive
verifications returned `insufficient_data` (n=1, then n=0, against a ≥30-trade
requirement). Backtesting replays only articles the system itself stored — free news tiers
offer no deep history — so the sample grows at wall-clock speed, and both existing windows
(2026-06-15→06-26, 2026-06-22→07-03) are already consumed for tuning. At projected yield
(~1 card/day post-fix, minus risk blocks), reaching the strategy doc's own "review after
100 trades" checkpoint takes months. The strategy is not just unproven; **as designed, it
is very slow to become provable**.

## Economics

Even after the blockers: $1,000 max notional means a 1% favorable move earns ~$10 against
~$3–4 round-trip costs and slippage — a ~0.35% per-trade cost hurdle. With a 2R target
sitting 3×ATR20 away, most swing trades will exit on the 5-day time stop rather than the
bracket, so realized P&L ≈ 5-day post-news drift minus costs. That can be positive if the
news selection has real predictive value at that horizon — but that is exactly the
unvalidated hypothesis. The risk controls themselves (latching $200/day kill-switch,
$120/trade risk, one position per instrument, $5k exposure cap) are sane and conservative.

## What is genuinely good

Persist-before-publish with publication obligations, fail-closed defaults throughout, the
paper/live port-mode double-check, working-order-counting risk gate, full per-decision
audit trail, the backtester invoking the executor's pure decision logic (live parity), and
the verify-backtest methodology — the last is the system's best asset, having already
caught every problem above with evidence.

## Recommendations (priority order)

1. **Resolve the risk-box / universe mismatch** — fractional shares, higher caps, or
   (cheapest) a generation-side tradeability filter
   (`price ≤ MAX_POSITION_SIZE`, `1.5·ATR ≤ risk_max_loss_usd`).
2. **Raise the regeneration token budget to ≥1.3M** for two-week windows and pre-filter
   the ~40% of LLM spend wasted on `instrument_not_subject` articles.
3. **Implement already-priced suppression** — deterministic gate and/or prompt
   instruction. It is the difference between a post-event-drift strategy and a
   buy-the-top machine.
4. **Run a full-coverage backtest on a holdout window and reach n≥30 closed trades**
   before tuning any other knob.
5. Mark `docs/trading-strategies.md` as stale or rewrite it to match the implementation;
   calibrate or drop the confidence gates.

Until those land, no statement about profitability — positive or negative — is supported
by evidence.

## Tracked issues

| Finding | Issue |
|---|---|
| 1 — risk-box / universe mismatch | [260709-01](../issues/issues-detail/260709-01.md) |
| 2 — already-priced suppression gap | [260709-03](../issues/issues-detail/260709-03.md) |
| 3 — evidence gate selects news-dense mega-caps | not tracked — config already widened; residual is a strategy observation to revisit once an n≥30 holdout sample exists |
| 4 — confidence gates never bind | [260709-04](../issues/issues-detail/260709-04.md) |
| 5 — trading-strategies.md stale | [260709-05](../issues/issues-detail/260709-05.md) |
| 6 — validation loop starved | visibility: [260708-01](../issues/issues-detail/260708-01.md) (pre-existing); wasted LLM spend: [260709-02](../issues/issues-detail/260709-02.md); budget default already raised to 5M (79d4b89) |
