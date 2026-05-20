# Product Constraint: No Thesis Card, No Trade

## 1. Constraint Statement

This product is defined by a single operating rule:

- Every trade must be backed by exactly one validated thesis card.
- If a thesis card is missing, incomplete, stale, or unapproved, trade execution is forbidden.

This is not a feature toggle. It is a product identity constraint and must remain always-on.

## 2. Thesis Card Contract

A thesis card is a structured object with mandatory fields:

1. Instrument (`ticker` + `exchange_code`) and direction.
2. Time horizon.
3. Exactly three evidence bullets sourced from recent news.
4. Confidence score.
5. Risk box containing max loss, stop condition, and invalidation condition.
6. Explicit decision state: `approved` or `rejected`.

### 2.1 Canonical JSON Shape

```json
{
  "card_id": "uuid",
  "ticker": "AAPL",
  "exchange_code": "XNAS",
  "direction": "buy",
  "time_horizon": "swing_1d_5d",
  "evidence": [
    {
      "bullet": "Supplier guidance upgrade indicates higher demand.",
      "article_id": "news-123",
      "event_id": "evt-earnings-guidance-20260520",
      "source": "finnhub",
      "published_at": "2026-05-20T08:30:00Z"
    },
    {
      "bullet": "Regulatory filing removes a near-term legal overhang.",
      "article_id": "news-124",
      "event_id": "evt-regulatory-filing-20260520",
      "source": "reuters-rss",
      "published_at": "2026-05-20T09:05:00Z"
    },
    {
      "bullet": "Peer earnings read-through supports sector momentum.",
      "article_id": "news-125",
      "event_id": "evt-sector-readthrough-20260520",
      "source": "marketaux",
      "published_at": "2026-05-20T09:20:00Z"
    }
  ],
  "confidence": 0.72,
  "risk_box": {
    "max_loss_usd": 120.0,
    "stop_condition": "close_below_20dma",
    "invalidation_condition": "guidance_reversal_or_negative_preannouncement"
  },
  "decision_state": "approved",
  "created_at": "2026-05-20T09:22:00Z",
  "expires_at": "2026-05-20T15:22:00Z"
}
```

## 3. Global Validation Rules

All components must enforce these invariants:

1. Instrument identity is the pair (`ticker`, `exchange_code`), not `ticker` alone.
2. `evidence` length must equal 3.
3. Each evidence bullet must reference an existing article id.
4. Evidence must contain exactly 3 unique `article_id` values (prevents duplicate coverage).
5. Confidence must be in range [0, 1].
6. `risk_box` fields are all mandatory.
7. `decision_state` must be either `approved` or `rejected`.
8. Approved cards require `expires_at` later than current time.
9. Only `approved` cards may proceed to execution.

Article-diversity rationale:

- Three unique articles force evidence rigor; rewrites of one story do not count.
- `event_id` is optional and retained for audit trail and future event clustering.
- Single-event trades are allowed when backed by multiple independent source perspectives.

## 4. Scope Guardrails

To keep the product focused, the following are out of scope unless they directly improve thesis-card quality, speed, or safety:

- Free-form chat that triggers trading decisions without a card.
- Broker execution paths that bypass card validation.
- Strategy-builder workflows that do not end in the canonical thesis-card contract.
- Non-actionable analytics views that are disconnected from card decisions.

## 5. Component Acceptance Criteria

### 5.1 NewsFetcher

- Must provide article quality and freshness needed to support exactly three evidence bullets.
- Must publish article identifiers that remain stable for card evidence references.

### 5.2 AnalyzerWorker

- Must create thesis cards using the canonical contract.
- Must fail closed when evidence count, risk box, or confidence validation fails.

### 5.3 TradeExecutor

- Must reject any execution request without a valid approved thesis card id.
- Must persist the thesis card reference for every decision and execution attempt.

### 5.4 UI

- Must present thesis cards as the primary decision surface.
- Must expose explicit approve or reject action per card.
- Must display rejection reason when card validation fails.

## 6. Litmus Test for New Features

A feature is shippable only if it improves at least one of:

1. Thesis-card clarity.
2. Thesis-card decision speed.
3. Thesis-card risk safety.

If not, the feature should not be added.