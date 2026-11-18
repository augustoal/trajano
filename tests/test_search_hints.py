"""HW9 regression: an empty search explains itself; a normal search is unchanged."""

from datetime import date, timedelta

from sqlmodel import Session

from data.seed import load_catalog
from trajano.domain import catalog
from trajano.schemas import SearchSpec

NEED_BY = date.today() + timedelta(days=60)


def _spec(**attributes: str) -> SearchSpec:
    return SearchSpec(
        length_mm=30, qty=500, ship_to="US-OH", need_by=NEED_BY, diameter="M8", **attributes
    )


def test_unsupported_material_names_the_value_and_the_alternatives(session: Session) -> None:
    load_catalog(session)
    session.commit()
    page = catalog.search(session, _spec(material="titanium", grade="Grade 5"))
    assert page.hits == [] and page.excluded == []
    by_field = {hint.field: hint for hint in page.hints}
    assert by_field["material"].value == "titanium"
    assert {"steel", "stainless"} <= set(by_field["material"].known_values)
    assert "8.8" in by_field["grade"].known_values


def test_wrong_case_is_explained_not_silently_empty(session: Session) -> None:
    load_catalog(session)
    session.commit()
    page = catalog.search(session, _spec(material="Steel"))
    assert page.hints and page.hints[0].field == "material"
    assert "steel" in page.hints[0].known_values


def test_impossible_combination_gets_one_spec_hint(session: Session) -> None:
    load_catalog(session)
    session.commit()
    page = catalog.search(session, _spec(material="steel", grade="A2-70"))
    assert [hint.field for hint in page.hints] == ["spec"]


def test_a_matching_search_has_no_hints(session: Session) -> None:
    load_catalog(session)
    session.commit()
    page = catalog.search(session, _spec(material="steel", grade="8.8", standard="ISO 4017"))
    assert page.hits or page.excluded
    assert page.hints == []
