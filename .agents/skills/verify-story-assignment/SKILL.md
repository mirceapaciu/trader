---
name: verify-story-assignment
description: Audit ThesisBuilder story assignments (article → evidence window / card placement) for cross-story errors, trace each error's blast radius through gates, windows, cards, and published signals, classify against known issues or file new ones, and maintain the labeled corpus that validates fixes. Use when the user asks to verify, audit, or check story/window/card assignment quality, or as the body of a recurring /loop.
---

# verify-story-assignment

Audit the article → story placement decisions ThesisBuilder makes (`t_story_assignments`),
find misassignments, trace their consequences, and keep a labeled corpus that turns fuzzy
LLM-judged findings into a deterministic regression suite for fixes.

**You are the judge.** Story-sameness has no deterministic oracle; you judge it in-context
from article text vs window narratives. Every confirmed finding must therefore be frozen into
the corpus (Stage 4) — the judge finds *new* errors, the *corpus* validates fixes. Never
validate a fix against your own fresh judgment alone.

**Known-defect catalog** (read before judging; do not re-file these):

| Issue | Defect | Signature |
|---|---|---|
| 260715-05 | Assignment taken from LLM on trust; cross-story `matched` placements | off-story article in a window, `assignment_source='matched'` |
| 260715-04 | Anchor gate key-scoped, runs before assignment; `new_story` indirect seeds anchor-less window | indirect-only collecting window; assignment `new_story` on a `customer_or_peer`/`supply_chain` analysis |
| 260709-07 | Soft dedupe per-source; same story via two feeds counts twice | two members of one window are the same event from `rss` + `finnhub` |

Fix status matters: once 260715-04/-05 land, their signatures become regressions (severity up,
not "known, skip"), and the fix may add audit columns/verdict rows this skill must start
reading — see Maintenance at the bottom.

## Input

Optional: an assignment-id range, a time window, or `fix` mode (Stage 5 enabled). Default:
incremental audit from the watermark, judge-and-report only — **no code changes in default
mode**.

## Protocol

### Stage 0 — Connect and load state

Connect to Postgres as in the fix-and-verify skill (load `.env.shared` → `.env.prod` →
`.env.secrets` via `load_env_files`, then psycopg). This skill reads only the
`thesis_builder` schema (dev-tool exception, read-only): `article_snapshot` on
`t_news_analyses` carries headline/summary, so no `news_fetcher` reads are needed. Never
write to any component schema.

State lives in `docs/verification-runs/story-assignment/`:
- `watermark.json` — `{"last_audited_assignment_id": N}`. Missing/0 ⇒ audit the full backlog.
- reports — `<YYMMDD>-<first_id>-<last_id>.md`, one per pass.

The corpus is `tests/product_components/thesis_builder/fixtures/story_assignment_corpus.jsonl`.
Read it before judging: past labels calibrate the rubric, and a case already in the corpus is
re-confirmed, not re-discovered.

### Stage 1 — Audit (always)

Pull unaudited assignments (cap one pass at ~200; if more are pending, audit the oldest 200,
advance the watermark, and let the next pass continue):

```sql
SELECT s.id, s.analysis_id, s.article_id, s.candidate_targets, s.chosen_target,
       s.assignment_source, s.error_code, s.created_at,
       a.ticker, a.subject_relation, a.validation_status, a.rejection_reason_code,
       a.article_snapshot->>'headline' AS headline,
       a.article_snapshot->>'summary'  AS summary
FROM thesis_builder.t_story_assignments s
JOIN thesis_builder.t_news_analyses a ON a.id = s.analysis_id
WHERE s.id > %(watermark)s
ORDER BY s.id
LIMIT 200
```

For each candidate target, load its narrative: `t_evidence_windows.story_narrative` for
`window:<id>`; the card's stored story narrative for `card:<id>`.

**Judgment rubric — "same underlying news event".** Match means the same announcement,
filing, report, or occurrence — the same event *instance*. Explicitly NOT sufficient: same
ticker, same sector/theme (two different AI-infrastructure deals are two stories), same
sentiment/direction/strategy, same companies in a different event. The calibration pair from
the seed corpus: "Nokia Unveils AI-Powered Network Platform in Major Nvidia Partnership" and
"Nokia and Nvidia Unveil AI-Native Radio Platform" are the SAME story (one announcement, two
feeds); "Saturn Cloud Partners With Lilac…" vs that Nokia narrative is a DIFFERENT story even
though both read through to NVDA via AI/GPU demand.

Verdict per assignment:
- `correct` — placement agrees with the rubric.
- `misassigned_matched` — `chosen_target` points at a window/card whose narrative is a
  different event (the 260715-05 class).
- `missed_match` — `new_story` chosen while a candidate narrative IS the same event
  (fragments stories into singletons; starves corroboration).
- `fallback_unverified` — `assignment_source='fallback'`: story-blind by construction; flag
  automatically, then judge whether the placement happened to be correct.
- `uncertain` — genuinely ambiguous from the available text. Record it, don't force it; do
  not add `uncertain` rows to the corpus.

Judge from article text and narratives only. Outcome data (did the card win?) is irrelevant
to assignment correctness and must not influence verdicts.

### Stage 2 — Trace blast radius (per non-correct finding)

Trace the full chain, as SQL over `thesis_builder.*`:

1. The analysis row: `subject_relation`, `validation_status`, `rejection_reason_code`,
   `validation_errors` (downgrade audit markers).
2. The destination window: members with their relations and headlines
   (`jsonb_array_elements_text(w.analysis_ids)` join), status, `status_reason`.
3. Did the window satisfy? Which card (`source_analysis_ids @> analysis id`)? Is the finding's
   evidence bullet on the card (`t_thesis_cards.evidence`)?
4. Did the signal publish (`signal_published_at`)? Did indirect evidence pass
   `indirect_no_anchor_evidence` because of a wrong-story anchor?

Severity, worst first: `published_signal` → `card_minted` → `window_satisfied_pending` →
`window_polluted` (bad member in a collecting window) → `contained` (quarantined, e.g.
indirect-only singleton window).

### Stage 3 — Classify and file

Match each finding against the known-defect catalog above (check each issue's current status
in `docs/issues/issues-index.md` first — a signature of a *resolved* issue is a regression
and must be called out as such, referencing the original issue).

- **Known open issue** ⇒ record as a new occurrence in the report; if it materially changes
  the issue's severity picture (e.g. first `published_signal` instance of a class thought
  contained), update the issue detail file's evidence section.
- **New failure class** ⇒ file an issue per AGENTS.md: entry in `docs/issues/issues-index.md`
  (status=new) + detail file under `docs/issues/issues-detail/` with problem statement,
  verified evidence (the Stage 2 trace), expected behavior, acceptance criteria, test plan.
  ID is `YYMMDD-XX`, next sequential XX for today.
- **Regression of a resolved issue** ⇒ update that issue's detail file and flag it at the top
  of the report; do not file a duplicate.

### Stage 4 — Corpus maintenance

Append every `correct`-with-teaching-value, `misassigned_matched`, `missed_match`, and
consequential `fallback_unverified` finding to the corpus. One JSON object per line, schema:

```json
{"case_id": "CS-0005", "labeled_at": "YYYY-MM-DD", "assignment_id": 0, "analysis_id": 0,
 "article_id": "cev_…", "ticker": "…", "subject_relation": "…",
 "headline": "…", "candidate_targets": ["…"], "chosen_target": "…",
 "assignment_source": "…", "target_narrative_headline": "…or null",
 "assignment_verdict": "correct|misassigned_matched|missed_match|fallback_unverified",
 "expected_target": "…", "expected_outcome": "one line: the correct end-state",
 "severity": "published_signal|card_minted|window_satisfied_pending|window_polluted|contained|null",
 "why": "one sentence", "related_issue": "YYMMDD-XX or null"}
```

Rules: rows are **self-contained** (headline and narrative inline — replayable and judgeable
without DB access); `case_id` is sequential `CS-NNNN`; never edit or delete an existing row —
if a label was wrong, append a superseding row with `"supersedes": "CS-NNNN"` and the
corrected verdict. Keep the corpus balanced: hard `correct` cases (like the Nokia two-feed
pair) matter as much as errors — they are what stops a fix from shattering every story into
singletons.

### Stage 5 — Fix (only in `fix` mode or on explicit user request)

Never enter this stage from a scheduled/loop run without the user having enabled it.

1. Branch off `main` — never commit fixes to `main` directly from this loop.
2. Minimal, targeted fix per fix-and-verify discipline. Respect open issues: if the finding
   is 260715-04/-05's class, implement per that issue's expected-behavior section rather than
   inventing a parallel mechanism.
3. Validate: unit tests for the touched module, then **corpus replay** (Stage 6). A fix is
   ready only when every corpus row's `expected_target`/`expected_outcome` holds and no
   `correct` row broke.
4. Stop at "branch ready + corpus green + funnel diff attached" and hand off for human
   review. Do not merge, do not mark issues resolved without the user.

### Stage 6 — Rerun (validates a fix; also run after someone else's fix lands)

Replay through the new code without touching production state:

- **Corpus replay:** for each corpus row, re-run the assignment decision path against the
  row's inline article text and narrative. Prefer a committed integration test
  (`tests/product_components/thesis_builder/`) that iterates the corpus file; write it on
  first need, then reuse. LLM-dependent steps use the same configured models as live.
- **Funnel diff:** regeneration backtest over the audited window into a `sim_bt_<run_id>`
  schema (Backtester regeneration mode), then diff assignment outcomes, window counts, and
  card counts against the live baseline. Analytic sweeps over the sim schema are free;
  reserve engine re-runs for confirming a chosen change. Note: prompt-text changes invalidate
  `backtester.t_llm_analysis_cache` keys — first re-run after a prompt change re-pays LLM
  cost; expected.
- Report: corpus pass/fail per row, funnel deltas (assignments by verdict class, windows,
  cards, published signals), and any `correct` rows that changed outcome.

### Reporting and watermark

Write the pass report to `docs/verification-runs/story-assignment/<YYMMDD>-<first>-<last>.md`:
counts by verdict, cross-story rate (misassigned_matched / matched), findings table
(assignment id, verdict, severity, issue, one-line why), corpus rows added, issues filed or
updated. Advance `watermark.json` only after the report is written.

End the session with:

```
## verify-story-assignment: ids <first>–<last>

**Audited:** <n> assignments (<n> matched / <n> new_story / <n> fallback)
**Errors:** <n> (<severity histogram>)
**Cross-story rate:** <x>% of matched (corpus baseline: <y>%)
**Corpus:** +<n> rows (now <total>)
**Issues:** <filed/updated/none>
**Report:** docs/verification-runs/story-assignment/<file>.md
```

## Running as a loop

`/loop /verify-story-assignment` (self-paced) or `/loop 30m /verify-story-assignment`.
Headless: `claude -p "/verify-story-assignment"` from Task Scheduler. Each pass is
incremental via the watermark; a pass with zero new assignments writes no report and just
says so. Requires local Postgres — this cannot run as a cloud routine. Fix mode is never
entered from a loop pass.

## Maintenance

When a fix for 260715-04/-05 lands: (1) move its catalog entry to "resolved — signature is
now a regression"; (2) extend the Stage 1 query with any new audit columns / verdict tables
the fix introduced (e.g. assignment-verification audit markers, `t_card_synthesis_verdicts`
rows) and judge against them; (3) re-run Stage 6 corpus replay once against the fixed code
and record the result in the next report. Updating this file is part of the fix's
definition-of-done.
