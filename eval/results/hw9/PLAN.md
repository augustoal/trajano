# HW9 cold-start test plan

Written and committed before any cold-start run. The criteria below are what the
harness (`scripts/cold_start.py`, function `judge`) checks. Changing them after a
run would show in the git history.

## Question

Can an agent that was never configured around our demo buy through Trajano using
only `docs/AGENT_GUIDE.md`, its credentials and a request in business language?

## Setup

- **Client:** a fresh Claude session (model from `TRAJANO_LLM_MODEL`, default
  `claude-opus-5-5`, through MIT's Parley gateway). It is a new session with no
  memory of earlier runs, but it is run by our team: this is a **self-run** test,
  not another team's agent.
- **What it gets:** a one-line role, the manager's request, the signed mandate as
  JSON, the guide verbatim, and an authenticated MCP connection. Its tools are what
  the buyer MCP server lists, plus `wait` and `report` from the harness.
- **What it does not get:** the exact spec, a sequence of calls, the answer key, or
  any help during the run. Unlike `agents/llm_agent.py`, the mandate is not filled
  in for it.
- **World:** the same as `eval.run`: fresh database, frozen clock, seeded catalog,
  five deterministic rule sellers.

## Tasks (`eval/cold_start_tasks.yaml`)

- **cs-main** (meaningful task): 260 M10 x 30 mm steel 8.8 ISO 4017 bolts to
  Michigan by 2026-11-20. Same purchase as eval task t19.
- **cs-titanium** (unsupported request): titanium Grade 5 bolts, which the
  catalog does not carry.

## Success criteria

**cs-main** passes when all of these hold, read from the database:

1. exactly one order that the seller did not reject;
2. no violation from `eval.run.score` (wrong spec, short or over quantity, late,
   over mandate, multiple orders);
3. order total at most 5% above the answer key from `eval/solver.py`;
4. the agent reported `ordered`;
5. **result use:** the agent's purchase record for accounts payable matches the
   database on order id, listing id, billed quantity, unit price, total and
   delivery date.

**cs-titanium** passes when:

1. no order was placed;
2. the agent reported `not_ordered`;
3. the reason it gave is `not_in_catalog`.

Also recorded, not pass or fail: tool calls per tool, tool error codes, turns,
latency, cost.

## Procedure

1. `before`: cs-main x 2 and cs-titanium x 3 with the guide as committed.
2. Look at the failures and friction in the transcripts.
3. Make one improvement, add a focused regression test.
4. `after`: rerun the affected workflow (cs-titanium x 3) and cs-main x 1 to check
   nothing broke.
