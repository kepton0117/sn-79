# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Each book's fundamental price as the state carries it.

The simulation engine writes every book's latest fundamental into the book itself (key `f`, read by miners as
`book.fundamental`) before the state goes to miners, and the reporting path reads the same values back from the
reported state's books, so the metrics publish what the state carried. A state whose books carry no key (the exchange
mechanism) reports the values the validator holds, which are empty there.
"""

BOOK_KEY = "f"


def _book_id(book_id):
    try:
        return int(book_id)
    except (TypeError, ValueError):
        return book_id


def stamp(books, prices) -> None:
    """Write each book's fundamental into the book; a book with no known price carries None."""
    prices = prices or {}
    for book_id, book in (books or {}).items():
        value = prices.get(_book_id(book_id))
        if isinstance(book, dict):
            book[BOOK_KEY] = value
        else:
            try:
                setattr(book, BOOK_KEY, value)
            except (AttributeError, TypeError, ValueError):
                pass


def carried(books):
    """The fundamental each book carried, by book id; None when no book carries the key."""
    out = {}
    found = False
    for book_id, book in (books or {}).items():
        if isinstance(book, dict):
            if BOOK_KEY in book:
                found = True
            out[_book_id(book_id)] = book.get(BOOK_KEY)
        else:
            value = getattr(book, BOOK_KEY, None)
            found = found or value is not None
            out[_book_id(book_id)] = value
    return out if found else None


def reported(books, held):
    """The fundamental the metrics publish for a state: what its books carried, else the values held."""
    values = carried(books)
    return values if values is not None else held
