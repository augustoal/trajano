from collections.abc import Iterator

import pytest
from sqlmodel import Session

from trajano.config import reset_settings
from trajano.db import get_engine, init_db, reset_clock, reset_engine


@pytest.fixture(autouse=True)
def fresh_database(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TRAJANO_DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("TRAJANO_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("TRAJANO_MANDATE_SIGNING_KEY", "test-mandate-key")
    reset_settings()
    reset_engine()
    reset_clock()
    init_db()
    yield
    reset_engine()
    reset_clock()
    reset_settings()


@pytest.fixture
def session() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as db:
        yield db
        db.rollback()
