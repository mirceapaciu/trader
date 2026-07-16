# verify-story-assignment — Stage 6 rerun: corpus replay of the 260715-05 fix

**Not an audit pass** (watermark untouched, still 66). This validates the just-landed fix
`dac97a8` (260715-05 assignment verification) against the labeled corpus produced by pass
260715-1-66. Triggered by explicit user request ("continue"), read-only, **no LLM cost**.

## Method

`dac97a8` added `_verify_story_assignment_target()` in `repository.py`: a **deterministic**
token-overlap gate. After the LLM picks a `matched` target, it tokenizes the incoming article
(`headline + summary + event_type + evidence_bullet_candidates`, minus the ticker symbol and a
32-word generic stoplist) and the target narrative, and **downgrades to `new_story` iff the two
token sets have zero overlap**. Because it is deterministic, it can be replayed over the corpus
without re-running the LLM.

Replay applied the **real** `_story_tokens()` / stoplist from `repository.py` to each of the 41
corpus rows, pulling article `headline + summary` from `t_news_analyses.article_snapshot` and
the **full** target narrative from the window/card. Pass/fail per row = does the verifier's
resolved target match the corpus `expected_target`?

**Faithfulness caveat — the replay is a conservative UPPER BOUND on the fix's catch rate.**
`event_type` is NULL on all these historical rows and `evidence_bullet_candidates` is not
persisted per-analysis, so the incoming side omits the bullets. A larger incoming set can only
*increase* overlap, which *reduces* downgrades. The production fix therefore catches **≤** what
is reported here.

## Result

| corpus bucket | verifier matches expected | reading |
|---|---|---|
| correct_new_story (3) | 3 / 3 | fine — `new_story` is skipped by the gate |
| correct_matched (7) | **7 / 7** | **no over-correction** — every legitimate match preserved |
| misassigned_matched (31) | **8 / 31** | **only ≤26% of known cross-story errors caught** |

The 23 misassignments that still pass, with their (incidental) overlap tokens:

| case | assn | ticker | overlap tokens |
|---|---|---|---|
| CS-0004 | 57 | NVDA | network, with |
| CS-0006 | 6 | NVDA | this |
| CS-0007 | 11 | GOOGL | billion |
| CS-0010 | 17 | AVGO | billion, broadcom, street, wall |
| CS-0011 | 18 | AMZN | amazon, will |
| CS-0013 | 21 | NVDA | price |
| CS-0015 | 24 | AVGO | significant |
| CS-0018 | 28 | GOOGL | billion |
| CS-0019 | 30 | LLY | from, lilly, million |
| CS-0020 | 31 | META | platforms, with |
| CS-0021 | 32 | MSFT | enterprise, microsoft |
| CS-0023 | 36 | NVDA | price |
| CS-0026 | 43 | MSFT | microsoft, significant |
| CS-0027 | 44 | AMZN | amazon, will |
| CS-0028 | 45 | AVGO | broadcom |
| CS-0032 | 49 | GOOGL | billion |
| CS-0034 | 52 | MSFT | microsoft |
| CS-0035 | 53 | MSFT | launch |
| CS-0036 | 58 | ORCL | cloud, oracle, price |
| CS-0037 | 59 | NVDA | nvidia, with |
| CS-0038 | 60 | NVDA | nvidia |
| CS-0040 | 64 | MSFT | microsoft, significant |
| CS-0041 | 65 | AVGO | 2026, broadcom, this |

Note CS-0004 (Saturn Cloud → Nokia/Nvidia window) — the original seed misassignment that
published a signal — still passes on `['network','with']`. The verifier does not catch the case
it was filed against.

## Root causes

1. **Only the ticker symbol is excluded, not the company/entity name.** `excluded_tokens =
   {result.ticker.lower()}` drops `nvda` but keeps `nvidia`, `broadcom`, `microsoft`, `amazon`,
   `oracle`, `lilly`. Two articles about the same watchlist company *always* share the company
   name, so the gate is a no-op for same-ticker cross-story matches — which is the entire
   260715-05 failure mode. (13 of 23 misses overlap on a company name.)
2. **Stoplist too small (32 tokens).** Weak tokens ≥4 chars slip through and create overlap on
   their own: `this`, `with`, `will`, `from`, `price`, `billion`, `significant`, `2026`,
   `launch`, `street`, `wall`. CS-0006 passes on `this` alone.
3. **Structural: bag-of-words overlap can't express "same event".** Same-company articles about
   *different* events share vocabulary by construction. A token-overlap floor is the wrong
   shape for story identity; a threshold tweak narrows but doesn't close the gap.

## Verdict

By the corpus standard the skill sets for a fix ("every corpus row's expected_target holds and
no correct row broke"), **`dac97a8` is not corpus-green: 23/31 error rows fail.** The added
integration tests (200 lines) pass because they are hand-picked; the realistic labeled corpus
is not satisfied. 260715-05 should be considered **not fully resolved**.

## Recommended next steps (need user go-ahead — not done here)

1. **Reopen 260715-05** (or file a follow-up) citing this replay.
2. Cheapest high-value fix: **exclude watchlist company names/aliases** (already available via
   the instrument registry aliases — see [[instrument-aliases-missing-short-names]]) from
   `_story_tokens`, and **expand the stoplist** (numbers, `this/with/will/from`, price/size
   words). Re-run this replay; expect a large jump in catch rate.
3. Recognize the structural ceiling: for `matched` verification, a token floor likely needs to
   be paired with an LLM confirm-the-event check or a stronger similarity signal. The corpus is
   now the regression gate for whichever direction is taken.
4. **Enrich the corpus schema** so this replay becomes an exact, committable LLM-free regression
   test: inline `summary` and (once persisted) `evidence_bullet_candidates` + `event_type` and
   the full target narrative in each row. Today's rows carry only headline + narrative headline,
   which is enough to *judge* but not to *replay the verifier exactly*.

## Drafted fix + re-replay (branch `fix/260715-05-entity-aware-story-verification`)

Per user request, drafted a minimal follow-up fix in `repository.py` and re-ran the replay.

**Change:**
1. `_entity_exclusion_tokens()` — new helper; excludes the subject instrument's own name
   tokens (from `instrument_display_name` + `instrument_aliases`, already threaded into
   `persist_analysis_and_update_evidence` by every runtime caller) from the overlap check, not
   just the ticker symbol. Threaded through `_update_window_and_maybe_create_card` →
   `_resolve_story_target` → both `_verify_story_assignment_target` call sites.
2. `_STORY_GENERIC_TOKENS` widened 32 → 99 (grammar fillers + generic finance/market vocab).
3. `_story_tokens()` now drops pure-numeric tokens (years, sizes).
4. Overlap rule left at **≥1** (see threshold analysis below).

Existing unit tests: **120 passed**.

**Re-replay result (same n=41 corpus, same upper-bound caveat):**

| bucket | before fix | after fix |
|---|---|---|
| correct_new_story | 3/3 | 3/3 |
| correct_matched | 7/7 | **7/7** (no over-correction) |
| misassigned_matched | ≤8/31 | **28/31** |

Threshold analysis — requiring **≥2** overlapping tokens catches 31/31 misassigned **but breaks
a correct match**: CS-0029 (a46, "Apple to spend $30B with Broadcom" → the Apple/Broadcom
partnership window) downgrades because `apple` is its only surviving token. Breaking a correct
match is the over-correction the skill forbids, so ≥2 is rejected and ≥1 retained.

**3 residual misses** (all single generic-domain-token overlaps — the structural ceiling):

| case | ticker | overlap | why bag-of-words can't tell |
|---|---|---|---|
| CS-0004 | NVDA | network | Saturn Cloud "GPU network" vs Nokia "network platform" |
| CS-0021 | MSFT | enterprise | Citi "enterprise AI" vs Frontier "enterprise deployment" |
| CS-0036 | ORCL | cloud | "Oracle down 60%" cloud story vs Oracle Japan-cloud contract |

Closing these needs an event-level signal (entity-pair or LLM confirm-the-event), not a token
tweak — adding `cloud`/`network`/`enterprise` to the stoplist would blind the gate on legitimate
stories. Recommend leaving them red and pairing the token floor with an event check in a
follow-up.

## Stage 6 summary

**Fix drafted:** branch `fix/260715-05-entity-aware-story-verification` (uncommitted; not merged)
**Before:** correct_matched 7/7, misassigned ≤8/31 · **After:** correct_matched 7/7, misassigned 28/31
**Rule:** overlap ≥1 kept (≥2 breaks correct match CS-0029) · unit tests 120 passed
**Corpus green?** Not fully — 3 residual reds are the bag-of-words ceiling; forcing them green over-corrects
**Root cause fixed:** company names now excluded (was ticker-symbol only) + stoplist 32→99 + numeric drop
**Handoff:** review branch; decide whether to (a) ship this as the 260715-05 top-up and reopen/annotate the issue, and (b) add an event-level check for the 3 residuals. A committable LLM-free regression test needs the corpus rows enriched with `summary` + full narrative (today they carry only headlines).
