"""Reset the database and load the synthetic catalog from data.catalog.

Each seller gets a company, one seller agent, and one store. The agents' API
keys are written to seed_keys.json (git-ignored), because a raw key exists
only at creation time.
"""

import json
from pathlib import Path

from sqlmodel import Session, SQLModel

from data.catalog import build
from trajano.db import get_engine, init_db
from trajano.domain.accounts import create_agent, create_company
from trajano.models import Listing, Store
from trajano.schemas import Role

KEYS_FILE = Path("seed_keys.json")


def load_catalog(session: Session) -> dict[str, str]:
    """Insert every seller and listing. Returns {seller name: raw API key}."""
    keys: dict[str, str] = {}
    for seller in build():
        company = create_company(session, seller.name, domain=seller.domain, verified=True)
        _agent, raw = create_agent(session, company.id or 0, f"{seller.name} agent", [Role.SELLER])
        keys[seller.name] = raw
        store = Store(company_id=company.id or 0, name=seller.store)
        session.add(store)
        session.flush()
        for row in seller.listings:
            data = {key: value for key, value in row.items() if key != "listing_id"}
            listing = Listing(store_id=store.id or 0, version=1, is_current=True, **data)
            session.add(listing)
            session.flush()
            if listing.id != row["listing_id"]:
                raise RuntimeError("load_catalog needs an empty database so listing ids match")
    return keys


def main() -> None:
    engine = get_engine()
    import trajano.models  # noqa: F401  (register tables)

    SQLModel.metadata.drop_all(engine)
    init_db()
    with Session(engine, expire_on_commit=False) as session:
        keys = load_catalog(session)
        session.commit()
    KEYS_FILE.write_text(json.dumps(keys, indent=2) + "\n")
    print(f"catalog loaded: {len(keys)} sellers; seller API keys in {KEYS_FILE}")


if __name__ == "__main__":
    main()
