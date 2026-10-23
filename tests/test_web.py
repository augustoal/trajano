from fastapi.testclient import TestClient

from tests.factories import make_buyer, make_listing, make_seller
from trajano.app import create_app


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
        assert "M8" in asked.text
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
