"""Pure quantity rules.

This module is intentionally dependency-free and side-effect free so that it can
be unit tested in isolation and reasoned about without reading anything else.

The quantity produced here is the number of ADDITIONAL likes to order, not a
desired final total. See README.md, section "Quantity rules".
"""

from __future__ import annotations

# Any observed comment with at least this many likes makes the formula return 0,
# which allows the scanner to stop early (see app/comment_finder.py).
ZERO_QUANTITY_THRESHOLD = 10_000

# Human-readable description of the tiers, rendered in the dashboard and README
# so the boundary behaviour is never a surprise.
QUANTITY_TIERS: tuple[tuple[str, str], ...] = (
    ("0 - 100", "150"),
    ("101 - 300", "500"),
    ("301 - 999", "floor(top_likes x 1.3)"),
    ("1000 - 2999", "top_likes x 2"),
    ("3000 - 7999", "top_likes + 1000"),
    ("8000 - 9999", "top_likes"),
    ("10000 and above", "0 (no order)"),
)


class InvalidLikeCount(ValueError):
    """Raised when a like count cannot be trusted as a nonnegative integer."""


def calculate_quantity(top_likes: int) -> int:
    """Return the number of additional likes to order for a given top_likes.

    This is the operator's exact function. Do not modify it: the decrease at the
    300 -> 301 boundary (500 -> 391) is intentional and must be preserved.
    """
    if top_likes >= 10000:
        return 0
    if top_likes <= 100:
        return 150
    if top_likes <= 300:
        return 500
    if top_likes < 1000:
        return int(top_likes * 1.3)
    elif top_likes < 3000:
        return int(top_likes * 2)
    elif top_likes < 8000:
        return top_likes + 1000
    else:
        return top_likes


def validate_like_count(value: object, *, field: str = "like_count") -> int:
    """Coerce a provider-supplied like count into a trusted nonnegative int.

    Missing data is NOT zero. Booleans, floats, None and malformed strings are
    rejected so that a bad provider payload can never manufacture an order.
    """
    if value is None:
        raise InvalidLikeCount(f"{field} is missing")
    if isinstance(value, bool):
        raise InvalidLikeCount(f"{field} is a boolean, not an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidLikeCount(f"{field} is an empty string")
        # Reject thousands separators, decimals, signs and units outright rather
        # than guessing what the provider meant.
        if not text.isdigit():
            raise InvalidLikeCount(f"{field} is not a plain integer string: {text!r}")
        parsed = int(text)
    else:
        raise InvalidLikeCount(f"{field} has unsupported type {type(value).__name__}")

    if parsed < 0:
        raise InvalidLikeCount(f"{field} is negative: {parsed}")
    return parsed


def safe_like_count(value: object) -> int | None:
    """Return a validated like count, or None when the value cannot be trusted."""
    try:
        return validate_like_count(value)
    except InvalidLikeCount:
        return None
