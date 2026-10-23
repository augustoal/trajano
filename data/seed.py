"""Create the database schema.

The synthetic catalog (5 sellers, about 200 bolt listings) is Mateo's.
`load_catalog` is the function he fills in. Until then this command only
creates empty tables so the server can start.
"""

from trajano.db import init_db


def load_catalog() -> None:
    raise NotImplementedError("Mateo: data.seed.load_catalog")


def main() -> None:
    init_db()
    print("schema ready; catalog not loaded yet")


if __name__ == "__main__":
    main()
