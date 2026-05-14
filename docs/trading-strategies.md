# Trading Strategies — News-Based Bot

## Overview

This document describes the trading strategies used by the news-based trading bot. All strategies operate on **US equities** in the bot's watchlist, using news sentiment as the primary signal. The strategies are designed for a small private trader with limited capital and a conservative risk profile.

---

## 1. Sentiment Momentum Strategy

**Core idea:** Trade in the direction of strong, confirmed news sentiment before the market fully prices it in.

### Signal Generation

1. The LLM analyzes each news article and produces a sentiment score from -1.0 (very bearish) to +1.0 (very bullish).
2. Scores from multiple articles about the same ticker are aggregated using a **recency-weighted average**:
   - Articles from the last 30 minutes: weight 1.0
   - Articles from 30–120 minutes ago: weight 0.5
   - Articles older than 2 hours: weight 0.2
3. The aggregated sentiment score must exceed a **threshold** to trigger a trade.

### Entry Rules

| Condition                                   | Action     |
|---------------------------------------------|------------|
| Aggregated sentiment ≥ +0.6, confidence ≥ 0.7 | **Buy**    |
| Aggregated sentiment ≤ -0.6, confidence ≥ 0.7 | **Sell / Short** |
| Sentiment between -0.6 and +0.6             | **Hold**   |

Additional entry conditions (all must be true):
- At least **2 independent articles** confirm the sentiment direction
- No conflicting high-confidence article in the opposite direction
- The ticker is not in cooldown (last trade < 30 minutes ago)
- The position would not exceed the max position size ($1,000)

### Exit Rules

- **Take profit:** Close position when unrealized profit reaches +3%
- **Stop loss:** Close position when unrealized loss reaches -1.5%
- **Time stop:** Close position after 4 hours if neither target is hit
- **Reversal exit:** Close immediately if new high-confidence news reverses the sentiment

### Order Type

- **Urgent signals** (urgency = "immediate"): Market order
- **Non-urgent signals** (urgency = "today"): Limit order at current bid/ask ± 0.1%

### Why This Works for a Small Trader

- News sentiment is often priced in within minutes for large caps but can take longer for mid-caps
- The multi-source confirmation requirement filters out noise
- Tight stop losses and time stops limit downside

---

## 2. Event-Driven Strategy

**Core idea:** React to specific, high-impact corporate events identified in news.

### Event Categories

| Event Type          | Expected Direction | Confidence | Typical Holding Period |
|---------------------|--------------------|------------|------------------------|
| Earnings beat       | Bullish            | High       | 1–2 days               |
| Earnings miss       | Bearish            | High       | 1–2 days               |
| FDA approval        | Bullish            | High       | 1–3 days               |
| FDA rejection       | Bearish            | Very high  | 1 day                  |
| CEO resignation     | Bearish (short-term) | Medium   | 1 day                  |
| Major partnership   | Bullish            | Medium     | 1–2 days               |
| Lawsuit / investigation | Bearish        | Medium     | 1–3 days               |
| Product launch      | Bullish            | Low–Medium | 1 day                  |
| Dividend increase   | Bullish            | Medium     | 1–2 days               |
| Stock split         | Neutral/Bullish    | Low        | Skip                   |

### Signal Generation

1. The LLM classifies each article into one of the event categories above (or "none").
2. Events with **High or Very High confidence** generate immediate trading signals.
3. Events with **Medium confidence** require a second confirming source.
4. Events with **Low confidence** are logged but not traded.

### Entry Rules

- Enter within **15 minutes** of event detection for high-confidence events
- Use **market orders** for high-urgency events (earnings, FDA)
- Use **limit orders** for medium-urgency events (partnerships, lawsuits)
- Position size: 50% of max position for medium confidence, 100% for high confidence

### Exit Rules

- **Earnings plays:** Close at next market open if entered after-hours, or after 1 trading day
- **FDA events:** Close after 1–3 days depending on price movement
- **Other events:** Use trailing stop of 2% once in profit; hard stop loss at -2%

### LLM Prompt Design

The LLM is instructed to extract:
```
1. Event type (from predefined list)
2. Affected ticker(s)
3. Expected price impact direction
4. Estimated magnitude (low / medium / high)
5. Confidence in the classification
6. Key facts supporting the assessment
```

---

## 3. Contrarian Reversal Strategy

**Core idea:** Fade extreme sentiment when it appears to be overblown or based on stale news.

### When to Apply

This strategy activates only when:
- A ticker has **5+ articles** with the same sentiment direction in the last 2 hours
- The stock price has already moved **> 3%** in the expected direction
- The most recent article is **> 30 minutes old** (momentum is fading)
- LLM assessment indicates the news is **"priced in"** or **"overreaction"**

### Entry Rules

| Condition                                          | Action              |
|----------------------------------------------------|---------------------|
| Extreme bullish sentiment + price up > 3% + fading | **Sell / Short**    |
| Extreme bearish sentiment + price down > 3% + fading | **Buy (reversal)** |

### Exit Rules

- **Take profit:** Close when price reverts 1.5% toward pre-news level
- **Stop loss:** Close at -2% (the move continued against us)
- **Time stop:** Close after 2 hours

### Risk Controls

This is the **highest risk** strategy. Additional safeguards:
- Never allocate more than **50% of max position size** ($500)
- Maximum **2 contrarian trades per day**
- Do **not** apply to earnings or FDA events (those trends often continue)
- Require LLM confidence ≥ 0.8 that the reaction is overblown

---

## 4. Sector Rotation Signal

**Core idea:** When news affects an entire sector, adjust exposure to sector peers.

### How It Works

1. When a major news event affects a sector leader (e.g., bad guidance from AAPL), the bot considers the impact on sector peers.
2. The LLM is asked: *"Does this news about [TICKER] have implications for [PEER_TICKERS] in the same sector?"*
3. If the LLM identifies spillover risk/opportunity with confidence ≥ 0.6, generate signals for the peers.

### Sector Mapping

Maintained in configuration:

```python
SECTOR_PEERS = {
    "AAPL": ["MSFT", "GOOGL", "META"],     # Big Tech
    "TSLA": ["RIVN", "NIO", "GM"],          # Automotive/EV
    "NVDA": ["AMD", "INTC", "TSM"],         # Semiconductors
    "AMZN": ["SHOP", "WMT", "TGT"],        # Retail/E-commerce
}
```

### Entry & Exit Rules

- Same as Sentiment Momentum, but with a **reduced position size** (50% of max)
- Only trade peers that are **in the watchlist**
- Require at least **medium confidence** from the LLM on the spillover effect

---

## 5. Strategy Selection & Blending

The bot does not use a single strategy in isolation. The `WorkflowOrchestrator` applies this priority:

```
1. Event-Driven (highest priority — specific, actionable events)
2. Sentiment Momentum (default — bread-and-butter strategy)
3. Sector Rotation (supplementary — extends reach of strong signals)
4. Contrarian Reversal (lowest priority — high risk, used sparingly)
```

### Conflict Resolution

When multiple strategies produce conflicting signals for the same ticker:

- **Event-Driven always wins** over other strategies
- If Sentiment Momentum says **buy** and Contrarian says **sell**: take no action (conflicting signals cancel out)
- If Sector Rotation confirms Sentiment Momentum direction: **increase confidence** by 0.1
- Log all conflicts for later review

---

## 6. Position Sizing

The bot uses a **fixed fractional** approach:

| Signal Strength                             | Position Size        |
|---------------------------------------------|----------------------|
| Strong (confidence ≥ 0.8, multiple sources) | 100% of max ($1,000) |
| Medium (confidence 0.6–0.8)                 | 50% of max ($500)    |
| Weak (confidence < 0.6)                     | No trade             |

**Portfolio-level constraints:**
- Total open positions must not exceed `MAX_PORTFOLIO_EXPOSURE` ($5,000)
- Maximum 3 positions in the same sector
- No single position > 20% of total portfolio exposure

---

## 7. Performance Tracking

The bot tracks and reports:

| Metric                    | Description                                   |
|---------------------------|-----------------------------------------------|
| Win rate                  | % of trades closed at a profit                |
| Average win / average loss| Ratio should be ≥ 1.5 for viability           |
| Profit factor             | Gross profits / gross losses (target ≥ 1.3)   |
| Max drawdown              | Largest peak-to-trough decline                |
| Strategy breakdown        | Win rate and P&L per strategy                 |
| Signal accuracy           | How often LLM sentiment matched price movement|
| Daily P&L                 | Net P&L per trading day                       |

### Strategy Tuning

- After **100 trades**, review per-strategy metrics
- If a strategy has a **win rate < 40%** or **profit factor < 1.0**, disable it
- Adjust sentiment thresholds in increments of 0.05 based on backtesting results
- Review and update LLM prompts quarterly

---

## 8. Backtesting

Before deploying any strategy with real money:

1. **Collect historical data:** Fetch 3–6 months of historical news from Finnhub/Marketaux
2. **Replay pipeline:** Run the full pipeline against historical news and price data
3. **Measure metrics:** Win rate, profit factor, max drawdown, Sharpe ratio
4. **Paper trade:** Run in IBKR paper mode for at least 2 weeks
5. **Go live:** Only after paper trading confirms positive expectancy

### Backtesting Caveats

- Historical LLM analysis may differ from real-time (news context changes)
- Slippage and execution delays are not perfectly modeled
- Survivorship bias: only currently-listed tickers are in the backtest
- Past sentiment patterns may not repeat

---

## 9. Strategy Summary

| Strategy              | Risk Level | Expected Frequency | Target Win Rate | Avg Holding Period |
|-----------------------|------------|--------------------|-----------------|--------------------|
| Sentiment Momentum    | Medium     | 3–5 trades/day     | 55–60%          | 1–4 hours          |
| Event-Driven          | Medium     | 1–2 trades/week    | 60–70%          | 1–2 days           |
| Contrarian Reversal   | High       | 0–2 trades/day     | 45–50%          | 1–2 hours          |
| Sector Rotation       | Medium     | 0–1 trades/day     | 50–55%          | 2–6 hours          |

**Overall target:** Net positive monthly returns after commissions, with max drawdown < 5% of portfolio.

---

## 10. Disclaimer

These strategies are for educational and personal use. Automated trading carries significant risk of loss. Past performance (including backtests) does not guarantee future results. The bot should be monitored regularly and the private trader should understand each strategy before enabling it with real capital.
