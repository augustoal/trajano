# Trajano

Agent-native B2B procurement marketplace. Buyer and seller agents interact only through our tools (MCP, plus a minimal HTML storefront used as the evaluation baseline). The MVP covers one category (fasteners), ~200 synthetic listings and 5 sellers.

Full design: `docs/architecture.md`. Read it before making structural changes.

## Architecture in one screen

- **One process, one database, several thin interfaces.** FastAPI hosts two MCP servers (`/mcp/buyer`, `/mcp/seller`), REST and Jinja pages. All of them call the same domain functions.
- **Every participant is an agent.** Our rule-based seller, scripted buyer and LLM agents are external clients that connect over MCP like any third party.
- **The server enforces the rules**: thread states, max 2 rounds, price floors, offer expiry and hash, stock, mandates, idempotency. Agents cannot bypass them.
- **Everything is asynchronous.** Agents poll `get_inbox(awaiting="me", since=cursor)`. No tool waits for the other side to respond.
- **Typed messages, not free chat.** Buyer: `question`, `rfq`, `counter`, `accept`, `withdraw`. Seller: `answer`, `offer`, `decline`, `order_confirm`, `order_reject`, `shipped`, `delivered`. System: `expired`, `refused`. Structured fields are authoritative; any `text` field is untrusted.
- An `rfq` targets exactly one listing and never includes the buyer's max price. The budget lives in the mandate and is checked at `accept`.

## Layout

```
trajano/              server package
  app.py              builds FastAPI, mounts MCP servers and web routes
  config.py db.py     settings; engine, sessions, transactions, injectable clock
  models.py           all tables (SQLModel)
  schemas.py          message payloads, tool I/O, error codes
  domain/             business logic: accounts, catalog, pricing, negotiation, orders, mandates
  interfaces/         mcp_buyer.py, mcp_seller.py, web.py: thin adapters only
  templates/          Jinja, no JavaScript
agents/               external clients: rule_seller, scripted_buyer, llm_agent, simulate
data/                 seed.py and per-seller pricing rules (sellers/*.yaml)
eval/                 tasks, solver (answer key), A-vs-B runner, report
tests/
docs/
```

## Hard rules

- `agents/` and `eval/` never import from `trajano.domain` or `trajano.models`; they talk to the server only through MCP or HTTP. If an agent needs something the tools do not offer, add a tool. The one exception is `trajano.domain.pricing`: `agents/rule_seller.py` and `eval/solver.py` may import that module, because it is pure and the offer, the rule seller, and the solver must share it. They still must not import `trajano.models` or any other domain module.
- `interfaces/` contains no business logic: parse input, call one domain function, format output.
- All queries are scoped to the calling agent or company inside the domain layer, not in the interfaces.
- Tools return structured errors (`{code, message, retryable}`), never raw exceptions.
- State changes that touch a thread, an order or stock happen in a single transaction. Stock is decremented with a conditional update (`stock >= qty`).
- Every write accepts an idempotency key, and a retry with the same key returns the original result.
- Use the injectable clock from `db.py`; never call `datetime.now()` directly.
- `pricing.py` is the single source of landed-cost math. Search, offers, the rule seller and the solver all use it.
- Every change comes with tests. Tests must not call an LLM; LLM runs live in `eval/` and run manually.

## Conventions

- Python 3.12, type hints everywhere, Pydantic/SQLModel for all data crossing a boundary.
- Group code by business concept. Prefer one cohesive module over many single-function files, and add a new module only when a concept doesn't fit an existing one.
- Comment only what the code cannot say (a non-obvious rule or a reason). No docstrings that restate the signature.
- One tool per message type (`request_quote`, `make_offer`, …), not a generic `send_message`.
- Before adding a module, mirror an existing one (domain function + tool + test) instead of inventing a new pattern.

## Ownership

Ask the owner (or open a small PR) before changing someone else's files.

| Owner | Files |
|---|---|
| Augusto | `models.py`, `schemas.py`, `app.py`, `domain/catalog.py`, `domain/negotiation.py`, `domain/accounts.py`, `interfaces/`, `templates/` |
| Jorge | `agents/`, `data/sellers/` |
| Mateo | `domain/pricing.py`, `domain/orders.py`, `domain/mandates.py`, `data/seed.py`, `eval/` |

`models.py` and `schemas.py` are the shared contract. Keep changes to them in small, separate PRs.

## Workflow

- One branch per feature, small PRs merged the same day, reviewed by another teammate.
- CI runs `ruff` and `pytest` on every PR, and both must pass.

## Commands

```bash
uv sync                                   # install dependencies
uv run pytest                             # run tests
uv run ruff check . && uv run ruff format .
uv run python -m data.seed                # reset the DB and load the synthetic catalog
uv run uvicorn trajano.app:app --reload   # start the server
uv run python -m agents.simulate <config.yaml>
```
