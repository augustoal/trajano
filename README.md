# Trajano

Agent-native B2B procurement marketplace for fasteners. Buyer and seller agents talk only through the MCP tools. A small HTML storefront is the evaluation baseline.

This repository is the shared foundation from [docs/architecture.md](docs/architecture.md) section 13, step 1. The contracts are in place, and one read path (`get_listing`) is built end to end. Search, negotiation writes, pricing, orders, mandates, the synthetic catalog, and the agents are typed stubs. Fill those in on the owning track. Do not invent a parallel REST API.

## Commands

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
uv run python -m data.seed
uv run uvicorn trajano.app:app --reload
uv run python -m agents.simulate <config.yaml>
```

`data.seed` creates the tables and stops. Loading the ~200 listings is Mateo's `load_catalog`.

Settings use the `TRAJANO_` prefix. The default database is `sqlite:///./trajano.db`. Dev defaults for the API-key pepper and the mandate signing key are not for production; the process warns when they are still the defaults.

## What is implemented

- Repo layout, `pyproject.toml`, ruff, pytest, and CI (`.github/workflows/ci.yml`) running both on every PR.
- `models.py` and `schemas.py`, the shared contract.
- Domain signatures for the rest of the surface. Each stub raises `NotImplementedError` naming its owner.
- Accounts that issue and check API keys: HMAC-SHA256 with the pepper, never stored raw. A bad or inactive key is `UNAUTHORIZED` and writes an audit row. A missing key is the same code and does not.
- `catalog.get_listing`: current listing, tiers sorted by `min_qty`, `price_floor` omitted, `seller_notes` returned and treated as untrusted.
- The thread state machine: `transition`, `next_actions`, `is_my_turn`, and the inbox cursor. `get_inbox` and `get_thread` work and expire stale offers lazily on read. `send` is still a stub.
- Store and listing writes: `create_store`, `create_listing`, `update_listing` (a new version per update), and `get_listing_for_seller`, all idempotent.
- Offer hash and mandate signing bytes. Golden values are locked in `tests/test_contract.py`.
- The idempotency helper (`begin` / `complete`). Message and order unique indexes are the race backstop.
- Both MCP servers, mounted at `/mcp/buyer` and `/mcp/seller`, and the storefront shell (login, listing, search form, threads, owner). Tools return `{code, message, retryable}` and do not raise raw exceptions.

## Who owns the stubs

| Owner | Still to build |
|---|---|
| Augusto | `catalog.search` (beyond the missing-field check), `negotiation.send`, rate limit, owner dashboard |
| Jorge | `agents/` decision logic and the seller YAML |
| Mateo | `pricing.landed_cost`, `orders.*`, `mandates.*`, `data.seed.load_catalog`, `eval/` |

`agents/` and `eval/` talk to the server over MCP or HTTP. The one exception is `trajano.domain.pricing`, which `agents/rule_seller.py` and `eval/solver.py` may import. They still must not import `trajano.models` or any other domain module. `pricing.py` must not import `models`.

## Copy this pattern

`get_listing` is the example:

1. `trajano/domain/catalog.py` — `get_listing` loads the row, hides the floor, returns `ListingPublic`.
2. `trajano/interfaces/mcp_buyer.py` — the tool parses nothing extra, calls that one function, and returns `GetListingResult`.
3. `trajano/interfaces/web.py` — the listing page calls the same function.
4. `tests/test_get_listing.py` and the listing half of `tests/test_web.py`.

An interface function parses input, calls one domain function, and formats the output. Domain functions take a `Session`, flush, and do not commit. `interfaces.support.attempt` commits, or rolls back and records an audit when the error names an action.

## Contract choices

These are the calls the foundation made where the architecture left a gap. Say so if one of them should change; `models.py` and `schemas.py` stay small, separate PRs after that.

- **MCP SDK.** Pinned to `mcp>=1.16,<2`. Version 2 renamed `FastMCP`. Servers are streamable HTTP, stateless, `json_response=True`, mounted at `/`. DNS-rebinding protection is off because the app is mounted behind FastAPI, which owns the host check. `Authorization: Bearer tj_...` is read from the tool-call request. The web cookie is `trajano_api_key` (HttpOnly, SameSite=Lax, 14 days).
- **Money.** `Decimal`, USD, unit prices to 4 decimal places, totals to 2, `ROUND_HALF_UP`. Lengths are millimeters. Diameter is a nominal string (`"M8"`). `ship_to` and `ships_from` are opaque strings.
- **Listings.** A new version is a new row. Unique `(store_id, sku, version)`, and one current row per `(store_id, sku)`. Offers point at the version's `id`. `price_floor` never appears on `ListingPublic`.
- **Threads.** `seller_company_id` is stored on the thread. Question and answer are legal in `OPEN`, `QUOTE_REQUESTED`, `OFFERED`, and `COUNTERED` and do not change state. An answer is offered only while `pending_question` is set. One counter (`rounds_used >= 1` drops `counter` from `next_actions`); the seller's reply to it is a final offer. `refused` is not a transition.
- **Offer hash.** SHA-256 of canonical JSON (sorted keys, no whitespace) of listing id and version, cost lines, unit price (4 dp), qty, total (2 dp), currency, ship-to, delivery date, `expires_at` at second precision (`YYYY-MM-DDTHH:MM:SSZ`, microseconds dropped), and `final`. The hash is not part of the hashed body.
- **Mandate bytes.** HMAC-SHA256, which Mateo implements, over canonical JSON of agent, company, category, currency, expiry, and the three limits. `id` and `signature` are excluded. An accept signature must be 64 hex characters.
- **Inbox cursor.** `YYYY-MM-DDTHH:MM:SS.ffffffZ|{thread_id}`, exclusive, ordered by `(updated_at, id)`.
- **Idempotency.** Same key and same fingerprint replays the stored JSON. A different fingerprint is `CONFLICT` and not retryable. A key still in progress is `CONFLICT` and retryable. System messages use sender id `0` in the unique index.
- **Clock.** `trajano.db.utc_now`. Tests freeze it. `datetime.now` is only allowed in `db.py`.
- **Search.** `search` rejects a spec that is missing `length_mm`, `ship_to`, `need_by`, or `qty`, in that order, with `SPEC_INCOMPLETE` and the message `missing a, b`. The rest waits on `pricing.landed_cost`. Results will sort by landed cost, then listing id, and will exclude on MOQ, stock, and date.
- **`mark_delivered`.** The seller message list in the architecture omitted it. The message type exists, and the rule is one tool per message type, so the seller server has it.
- **Seller order tools.** Confirm, reject, ship, and deliver call `negotiation.send` only. The domain send path is what calls into orders, in the same transaction.
