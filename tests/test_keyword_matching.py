import pytest

from app.comment_finder import build_keyword_pattern, matches_keyword, normalize_text

PATTERN = build_keyword_pattern("Mael Vorran")


@pytest.mark.parametrize(
    "text",
    [
        "Mael Vorran",
        "mael vorran",
        "MAEL VORRAN",
        "MaEl VoRrAn",
        "  mael   vorran  ",
        "Mael\tVorran",
        "Mael\nVorran",
        "read Mael Vorran!",
        "(Mael Vorran)",
        "@Mael Vorran",
        "\u201cMael Vorran\u201d is great",
        "loved it. mael vorran, obviously.",
        "\uff2d\uff41\uff45\uff4c \uff36\uff4f\uff52\uff52\uff41\uff4e",  # fullwidth, NFKC folds it
        "Mael\u00a0Vorran",  # non-breaking space
    ],
)
def test_matches(text):
    assert matches_keyword(text, PATTERN) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Mael",
        "Vorran",
        "Maelvorran",
        "MaelVorran",
        "Mael Vorrano",
        "xMael Vorran",
        "Mael Vorranx",
        "Vorran Mael",
        "Mael-Vorran",
        "Mael. Vorran",
        "premael vorran",
        "Michael Vorran",
        "Mael Vorra",
    ],
)
def test_does_not_match(text):
    assert matches_keyword(text, PATTERN) is False


def test_normalization_does_not_mutate_the_original():
    original = "  Mael   VORRAN  "
    assert normalize_text(original) == "mael vorran"
    assert original == "  Mael   VORRAN  "


def test_empty_keyword_is_rejected():
    with pytest.raises(ValueError):
        build_keyword_pattern("   ")


def test_keyword_is_configurable():
    other = build_keyword_pattern("Ana Kovac")
    assert matches_keyword("ANA KOVAC rocks", other) is True
    assert matches_keyword("Mael Vorran rocks", other) is False
