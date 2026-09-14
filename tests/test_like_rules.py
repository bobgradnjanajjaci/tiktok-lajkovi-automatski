import pytest

from app.like_rules import InvalidLikeCount, calculate_quantity, safe_like_count, validate_like_count

# Every boundary from the specification, verbatim.
BOUNDARIES = [
    (0, 150),
    (99, 150),
    (100, 150),
    (101, 500),
    (249, 500),
    (280, 500),
    (299, 500),
    (300, 500),
    (301, 391),
    (999, 1298),
    (1000, 2000),
    (2999, 5998),
    (3000, 4000),
    (7999, 8999),
    (8000, 8000),
    (9999, 9999),
    (10000, 0),
    (15000, 0),
]


@pytest.mark.parametrize("top_likes,expected", BOUNDARIES)
def test_quantity_boundaries(top_likes, expected):
    assert calculate_quantity(top_likes) == expected


def test_the_300_to_301_step_down_is_preserved():
    """300 orders 500 and 301 orders 391. The decrease is intentional."""
    assert calculate_quantity(300) == 500
    assert calculate_quantity(301) == 391
    assert calculate_quantity(301) < calculate_quantity(300)


def test_quantity_is_never_negative_and_zero_only_above_threshold():
    for top_likes in range(0, 12000, 37):
        quantity = calculate_quantity(top_likes)
        assert quantity >= 0
        assert (quantity == 0) == (top_likes >= 10000)


@pytest.mark.parametrize(
    "value",
    [None, True, False, -1, "-5", "", "  ", "12.0", "1,200", "1e3", "1 200", "abc", 3.7, [], {}],
)
def test_invalid_like_counts_are_rejected(value):
    with pytest.raises(InvalidLikeCount):
        validate_like_count(value)
    assert safe_like_count(value) is None


@pytest.mark.parametrize("value,expected", [(0, 0), (7, 7), ("0", 0), ("4213", 4213)])
def test_valid_like_counts(value, expected):
    assert validate_like_count(value) == expected


def test_missing_data_is_not_zero():
    assert safe_like_count(None) is None
    assert safe_like_count(None) != 0
