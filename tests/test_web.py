import re

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.factories import make_buyer, make_listing, make_seller
from tests.test_deal import NEED_BY, NOW, _draft, _mandate, _offer
from trajano.app import create_app
from trajano.db import freeze_clock, get_engine
from trajano.models import Order, Thread
from trajano.schemas import ThreadStatus


def test_health_and_login_gate(session) -> None:
    _seller, _key, store = make_seller(session)
    listing = make_listing(session, store)
    buyer, key = make_buyer(session)
    session.commit()
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}
        hidden = client.get(f"/listing/{listing.id}", follow_redirects=False)
        assert hidden.status_code == 303
        assert hidden.headers["location"].startswith("/login")

        bad = client.post("/login", data={"api_key": "tj_nope", "next": f"/listing/{listing.id}"})
        assert bad.status_code == 401

        hijack = client.post(
            "/login",
            data={"api_key": key, "next": "https://evil.example/steal"},
            follow_redirects=False,
        )
        assert hijack.status_code == 303
        assert hijack.headers["location"] == "/"
        assert "httponly" in hijack.headers["set-cookie"].lower()

        client.post("/login", data={"api_key": key, "next": f"/listing/{listing.id}"})
        page = client.get(f"/listing/{listing.id}")
        assert page.status_code == 200
        assert "M8" in page.text
        assert "buy 2000" in page.text
        assert "<script>" not in page.text
        assert "0.0500" not in page.text
        assert buyer.name in page.text

        home = client.get("/")
        assert home.status_code == 200
        incomplete = client.get("/", params={"qty": "10", "ship_to": "US-CA"})
        assert "SPEC_INCOMPLETE" in incomplete.text
        assert "missing length_mm, need_by" in incomplete.text
        assert buyer.name in incomplete.text

        asked = client.post(
            f"/listing/{listing.id}/question",
            data={"text": "Is this zinc plated?", "idempotency_key": "web-q-1"},
        )
        assert asked.status_code == 200
        assert asked.url.path.startswith("/threads/")
        assert "Is this zinc plated?" in asked.text
        assert buyer.name in asked.text
        assert "<script>" not in asked.text

        # An OPEN thread is still the buyer's move: they can request a quote.
        threads = client.get("/threads")
        assert threads.status_code == 200
        assert "OPEN" in threads.text
        owner = client.get("/owner")
        assert owner.status_code == 200
        assert buyer.name in owner.text

        client.post("/logout")
        again = client.get(f"/listing/{listing.id}", follow_redirects=False)
        assert again.status_code == 303


def _hidden(page: str, name: str) -> str:
    # The latest form on the page wins; the thread page renders accept before counter.
    return re.findall(rf'name="{name}" value="([^"]*)"', page)[-1]


def _thread_id(response) -> int:
    return int(response.url.path.rsplit("/", 1)[1])


def test_a_buyer_can_negotiate_and_buy_through_the_storefront(session) -> None:
    freeze_clock(NOW)
    seller, _seller_key, store = make_seller(session)
    listing = make_listing(session, store)
    buyer, key = make_buyer(session)
    mandate = _mandate(session, buyer)
    session.commit()
    with TestClient(create_app()) as client:
        client.post("/login", data={"api_key": key})
        quoted = client.post(
            f"/listing/{listing.id}/quote",
            data={
                "qty": "230",
                "ship_to": "US-CA",
                "need_by": NEED_BY.isoformat(),
                "idempotency_key": "web-rfq-1",
            },
        )
        assert quoted.status_code == 200
        thread_id = _thread_id(quoted)
        assert "QUOTE_REQUESTED" in quoted.text
        assert "Accept offer" not in quoted.text

        with Session(get_engine(), expire_on_commit=False) as db:
            _offer(db, seller, thread_id, _draft(listing), key="web-offer-1")
            db.commit()

        page = client.get(f"/threads/{thread_id}")
        assert "Accept offer" in page.text
        assert "Send counter" in page.text
        assert "Withdraw" in page.text
        assert str(_draft(listing).total) in page.text

        countered = client.post(
            f"/threads/{thread_id}/counter",
            data={
                "offer_id": _hidden(page.text, "offer_id"),
                "unit_price": "0.1000",
                "idempotency_key": "web-counter-1",
            },
        )
        assert countered.url.path == f"/threads/{thread_id}"
        assert "COUNTERED" in countered.text
        assert "Send counter" not in countered.text

        with Session(get_engine(), expire_on_commit=False) as db:
            final = _draft(listing, unit_price=_draft(listing).unit_price)
            _offer(db, seller, thread_id, final, key="web-offer-2")
            db.commit()

        page = client.get(f"/threads/{thread_id}")
        assert "Send counter" not in page.text
        form = {
            "offer_id": _hidden(page.text, "offer_id"),
            "offer_hash": _hidden(page.text, "offer_hash"),
            "idempotency_key": "web-accept-1",
        }

        garbled = client.post(f"/threads/{thread_id}/accept", data={**form, "mandate": "{"})
        assert garbled.status_code == 400
        assert "VALIDATION" in garbled.text
        assert "Accept offer" in garbled.text

        tampered = client.post(
            f"/threads/{thread_id}/accept",
            data={**form, "offer_hash": "0" * 64, "mandate": mandate.model_dump_json()},
        )
        assert "STALE_OFFER" in tampered.text

        accepted = client.post(
            f"/threads/{thread_id}/accept", data={**form, "mandate": mandate.model_dump_json()}
        )
        assert accepted.url.path == f"/threads/{thread_id}"
        assert "order_id" in accepted.text
        assert "Accept offer" not in accepted.text

        again = client.post(
            f"/threads/{thread_id}/accept", data={**form, "mandate": mandate.model_dump_json()}
        )
        assert again.status_code == 200

    with Session(get_engine()) as db:
        assert len(db.exec(select(Order).where(Order.buyer_agent_id == buyer.id)).all()) == 1


def test_a_buyer_can_withdraw_from_the_storefront(session) -> None:
    freeze_clock(NOW)
    seller, _seller_key, store = make_seller(session)
    listing = make_listing(session, store)
    buyer, key = make_buyer(session)
    session.commit()
    with TestClient(create_app()) as client:
        client.post("/login", data={"api_key": key})
        quoted = client.post(
            f"/listing/{listing.id}/quote",
            data={
                "qty": "230",
                "ship_to": "US-CA",
                "need_by": NEED_BY.isoformat(),
                "idempotency_key": "web-rfq-2",
            },
        )
        thread_id = _thread_id(quoted)
        with Session(get_engine(), expire_on_commit=False) as db:
            _offer(db, seller, thread_id, _draft(listing), key="web-offer-3")
            db.commit()

        empty = client.post(
            f"/threads/{thread_id}/counter",
            data={
                "offer_id": _hidden(client.get(f"/threads/{thread_id}").text, "offer_id"),
                "idempotency_key": "web-counter-2",
            },
        )
        assert empty.status_code == 400
        assert "unit_price or qty is required" in empty.text

        withdrawn = client.post(
            f"/threads/{thread_id}/withdraw",
            data={"reason_code": "PRICE", "idempotency_key": "web-withdraw-1"},
        )
        assert withdrawn.url.path == f"/threads/{thread_id}"
        assert "Withdraw</button>" not in withdrawn.text

        stranger_thread = client.post(
            f"/threads/{thread_id + 999}/withdraw",
            data={"reason_code": "PRICE", "idempotency_key": "web-withdraw-2"},
        )
        assert stranger_thread.status_code == 404

    with Session(get_engine()) as db:
        thread = db.get(Thread, thread_id)
        assert thread is not None
        assert thread.status is ThreadStatus.WITHDRAWN
