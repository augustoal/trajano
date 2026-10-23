from trajano.schemas import Breakdown, PriceableListing


def landed_cost(listing: PriceableListing, qty: int, ship_to: str) -> Breakdown:
    """Pack rounding, tier, freight by zone, and tax.

    Mateo owns the math. Search, offers, the rule seller, and the solver all
    call this so they compute the same number. This module stays free of the
    database and the clock.
    """
    raise NotImplementedError("Mateo: pricing.landed_cost")
