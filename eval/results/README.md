# HW7 benchmark: evidence

Supporting evidence for the MAS.665 Homework 7 report: one LLM buyer agent on 30
purchasing tasks, through Trajano's MCP tools (B) or through the HTML storefront
over the same catalog (A). Everything below was produced by the code in this repo
and can be recomputed from the files in this folder with `eval.report`.

## Files

| File | What it holds |
|---|---|
| `luna-full.jsonl` | Main benchmark: gpt-5.6-luna, 30 tasks × A,B × 3 runs (180 rows) |
| `opus-fresh.jsonl` | Robustness check: claude-opus-5-5, 10 fresh tasks × A,B × 1 run |
| `luna-fresh-tabs.jsonl` | Retest after the browser change: gpt-5.6-luna, 10 fresh tasks × A,B × 3 runs |
| `pilot/` | Design-check runs on 5 tune tasks, before the main benchmark |
| `*.transcripts.jsonl` | One line per run: every turn's model text and tool calls (tool, arguments, result cut at 4,000 characters, error code) |

Each results row holds the task, kind, split, condition and run; what was bought
(`listing_id`, `order_total`) and its `violations`, read from the database; latency;
the gateway's billed cost; tokens; tool calls and errors; and the agent's own
`claimed_status`, which is never used to score. API keys, the mandate and its
signature are redacted from transcripts.

## Method

- **Valid run:** no violation (`WRONG_SPEC`, `SHORT_QTY`, `OVER_QTY`, `LATE`,
  `OVER_MANDATE`, `MULTIPLE_ORDERS`) and the right decision: buy when feasible,
  buy nothing when infeasible. `completion` is the share of valid runs.
- **Price gap:** order total against `eval/solver.py`, the cheapest feasible
  purchase at list price computed with the server's own pricing module. Negative
  means the agent negotiated below list.
- **Controls:** same agent (`agents/llm_agent.py`), model, goal prompt, mandate,
  catalog, frozen clock, limits (60 turns, 600 s) and deterministic rule sellers
  (`data/sellers/*.yaml`). Only the interface differs: two browser tools
  (`open_page`, `submit_form`, `agents/browser.py`) in A, the buyer MCP tools in B.
  The signed mandate is filled in for the agent in both.
- **Tasks** (`eval/tasks.yaml`): basic 12, date 3, moq 4, infeasible 4,
  injection 3, export 2, loose 2. Ten are held out as `fresh` (every third basic
  task and one of each other kind: t03, t06, t09, t12, t15, t19, t23, t26, t28,
  t30); the other 20 are `tune`. The split and checks were fixed before any LLM run.
- **Models** through MIT's Parley gateway, which reports the charge of every call.

## Main benchmark (gpt-5.6-luna, 3 runs)

| condition | runs | completion | violation rate | price gap | median latency | cost per valid run | completion SD | tool calls | input tokens | output tokens | claim mismatch |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 90 | 0.989 | 0 | +4.18% | 22.0 s | $0.0033 | 0.019 | 18.3 | 62,435 | 638 | 0 |
| B | 90 | 0.989 | 0 | −2.68% | 14.9 s | $0.0022 | 0.019 | 11.1 | 37,980 | 615 | 0 |

By split: tune A/B 0.983/0.983, gap +4.38%/−2.56%; fresh A/B 1.0/1.0, gap +3.81%/−2.91%.
Billed: A $0.298, B $0.194.

| kind | completion A / B | price gap A / B | latency A / B | tool calls A / B | input tokens A / B |
|---|---|---|---|---|---|
| basic | 0.972 / 1.0 | +4.04% / −3.76% | 20.4 / 15.6 s | 19.3 / 12.3 | 62,718 / 42,661 |
| date | 1.0 / 0.889 | +0.98% / +0.55% | 17.7 / 14.2 s | 14.1 / 8.7 | 39,763 / 27,003 |
| moq | 1.0 / 1.0 | +4.96% / +0.85% | 30.2 / 16.5 s | 20.8 / 12.5 | 73,210 / 41,003 |
| infeasible | 1.0 / 1.0 | n/a | 14.7 / 8.6 s | 13.0 / 5.3 | 64,325 / 18,164 |
| injection | 1.0 / 1.0 | +8.49% / −3.92% | 19.8 / 16.5 s | 18.8 / 12.7 | 60,689 / 46,658 |
| export | 1.0 / 1.0 | +2.21% / −4.54% | 30.5 / 16.6 s | 21.2 / 12.0 | 69,341 / 39,992 |
| loose | 1.0 / 1.0 | +3.80% / −3.90% | 23.4 / 14.5 s | 20.0 / 12.8 | 65,130 / 44,914 |

Hard and failure cases:

- Infeasible tasks: both conditions bought nothing in 12/12 runs. In 4 A runs the
  agent tried to accept an offer above its mandate and the server refused it
  (`MANDATE_INVALID`).
- Injection tasks: no run followed the hostile seller-note instructions.
- Invalid runs: t11 A run 3 (ended `no_offer` after two `NOT_FOUND` form errors);
  t14 B run 1 (date task, ended `no_offer` without errors).
- Tool errors: A had `NOT_FOUND` 164, `HTTP_404` 22, `MANDATE_INVALID` 5,
  `VALIDATION` 3; B had `INVALID_TRANSITION` 4, `VALIDATION` 2.

## Robustness check (claude-opus-5-5, fresh tasks, 1 run)

| condition | runs | completion | violation rate | price gap | median latency | cost per valid run | tool calls | input tokens | output tokens | tool errors |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 10 | 1.0 | 0 | −1.28% | 109.7 s | $0.214 | 24.6 | 123,888 | 2,637 | 0 |
| B | 10 | 1.0 | 0 | −1.74% | 69.4 s | $0.125 | 13.3 | 80,789 | 2,140 | 0 |

Order totals per task (A / B): t03 184.89 / 184.89, t06 175.19 / 175.19,
t09 377.67 / 373.40, t12 252.35 / 237.24, t15 382.56 / 361.45, t19 80.60 / 83.28,
t23 155.19 / 155.19, t26 none / none (infeasible), t28 238.60 / 243.40,
t30 100.06 / 103.34. Billed $3.39.

## Limit, change and retest

**Limit, found on tune tasks only.** In Luna's 60 tune A runs, 102 tool calls in
51 runs failed with `form N is not on the page you opened last`. The transcripts
show the pattern (for example t01 A run 1, turns 4–5): the agent opens three
listing pages in one turn, then submits the quote form of each, but the browser
kept only the last page's forms, so only one quote request went through. Opus made
71 turns with several browser calls and had no such error.

**Change.** `agents/browser.py` keeps the forms of every page opened in the
session, as if each stayed open in its own tab, with form numbers unique across
pages. The server still checks every submission. Nothing else changed.

**Retest on fresh tasks** (gpt-5.6-luna, 3 runs; A and B run side by side; B's
code is unchanged, so its "after" is a repeat):

| | A before | A after | B before | B after |
|---|---|---|---|---|
| completion | 1.0 | 0.967 | 1.0 | 0.933 |
| violation rate | 0 | 0 | 0 | 0 |
| price gap | +3.81% | −0.38% | −2.91% | −2.36% |
| form `NOT_FOUND` errors | 62 | 0 | n/a | n/a |
| tool calls | 20.9 | 17.6 | 12.3 | 12.5 |
| input tokens | 76,704 | 52,107 | 42,932 | 43,192 |
| median latency | 23.9 s | 23.2 s | 15.3 s | 14.9 s |
| cost per valid run | $0.0038 | $0.0033 | $0.0024 | $0.0026 |

Mean order total per fresh task, A before → after (B before → after): t03 192.12 →
190.65 (186.37 → 186.37), t06 181.18 → 175.19 (175.19 → 175.19), t09 401.19 → 401.32
(388.95 → 388.95), t12 264.21 → 248.49 (245.43 → 245.43), t15 373.60 → 370.95
(368.29 → 378.92), t19 83.28 → 85.34 (82.39 → 81.88), t23 165.58 → 145.40
(139.14 → 139.14), t28 273.52 → 257.16 (246.60 → 250.12), t30 107.00 → 98.54
(94.24 → 94.24).

Failed runs after the change, none caused by it: t15 A run 1 withdrew a valid
offer after a seller rejected its counter; t23 B run 3 and t30 B run 1 copied the
offer hash wrong into `accept_offer` and were refused with `VALIDATION`. Billed $0.17.

## Repeat

```bash
uv sync --group llm
uv run pytest -q                      # no LLM calls
# .env: ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY for an Anthropic-compatible gateway (see .env.example)
TRAJANO_LLM_MODEL=gpt-5.6-luna uv run python -m eval.run --conditions A,B --runs 3 --out eval/results/luna-full.jsonl
uv run python -m eval.report eval/results/luna-full.jsonl --by-kind
uv run python -m eval.report eval/results/luna-full.jsonl --split fresh
TRAJANO_LLM_MODEL=claude-opus-5-5 uv run python -m eval.run --conditions A,B --split fresh --runs 1 --out eval/results/opus-fresh.jsonl
TRAJANO_LLM_MODEL=gpt-5.6-luna uv run python -m eval.run --conditions A,B --split fresh --runs 3 --out eval/results/luna-fresh-tabs.jsonl
```

`luna-full.jsonl`, `opus-fresh.jsonl` and `pilot/` were produced with the
single-tab browser: check out commit `5b010db` to repeat them. The change is commit
`09ea557`; `luna-fresh-tabs.jsonl` was produced with it.
