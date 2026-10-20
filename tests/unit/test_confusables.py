"""Lookalike / confusable name detection (marketplace v2 §4.5)."""

from __future__ import annotations

import pytest

from sessionfs.skills.confusables import (
    BRAND_RESERVED,
    NAMESPACE_CLAIM_RE,
    ProtectedName,
    check_confusable,
    damerau_levenshtein,
    has_mixed_scripts,
    skeleton,
)


@pytest.mark.parametrize(
    "lookalike",
    [
        "аnthropic",  # Cyrillic а
        "anthrоpic",  # Cyrillic о
        "anthropiс",  # Cyrillic с
        "Αnthropic",  # Greek capital alpha
        "anthrοpic",  # Greek omicron
        "ＡＮＴＨＲＯＰＩＣ",  # fullwidth (NFKC)
        "anthr0pic",  # digit zero for o
        "anthropic​",  # zero-width space
        "an­thropic",  # soft hyphen
        "anthrópic",  # combining accent
        "an-thropic",
        "anth_ropic",
        "an.thro.pic",
        "ANTHROPIC",
    ],
)
def test_lookalikes_share_the_skeleton(lookalike: str) -> None:
    assert skeleton(lookalike) == skeleton("anthropic") == "anthropic"


@pytest.mark.parametrize(
    "left,right",
    [
        ("paypal", "paypa1"),  # l / 1
        ("google", "g00gle"),  # o / 0
        ("acme", "acrne"),  # rn / m
        ("wise", "vvise"),  # vv / w
        ("dash", "clash"),  # cl / d
        ("sessionfs", "5essionfs"),  # 5 / s
        ("bebop", "8ebop"),  # 8 / b
        ("eel", "3el"),  # 3 / e
        ("kubernetes", "kubernetes"),
    ],
)
def test_digit_letter_and_multi_char_swaps(left: str, right: str) -> None:
    assert skeleton(left) == skeleton(right)


@pytest.mark.parametrize(
    "left,right",
    [("acme", "acne"), ("review-helper", "review-helpers"), ("openai", "opendata"), ("mail", "mall")],
)
def test_distinct_names_stay_distinct(left: str, right: str) -> None:
    assert skeleton(left) != skeleton(right)


def test_mixed_script_detection() -> None:
    assert has_mixed_scripts("Аnthropic")  # Cyrillic + Latin
    assert has_mixed_scripts("Anthropiс Inc")
    assert has_mixed_scripts("Gοοgle")  # Greek omicrons
    assert not has_mixed_scripts("Anthropic Inc.")
    assert not has_mixed_scripts("Ｆｕｌｌｗｉｄｔｈ Labs")  # NFKC folds to Latin
    assert not has_mixed_scripts("Яндекс")  # single script is fine
    assert not has_mixed_scripts("acme-2 tools")


def test_damerau_levenshtein() -> None:
    assert damerau_levenshtein("anthropic", "anthropic") == 0
    assert damerau_levenshtein("anthropic", "anthropci") == 1  # transposition
    assert damerau_levenshtein("anthropic", "antropic") == 1  # deletion
    assert damerau_levenshtein("anthropic", "anthropicx") == 1
    assert damerau_levenshtein("anthropic", "antrpic") == 2
    assert damerau_levenshtein("abc", "xyzuvw", limit=2) == 3  # capped


def _protected(*names: str, owner: str = "org:acme", strong: bool = True) -> list[ProtectedName]:
    return [ProtectedName(name, skeleton(name), owner, "publisher", strong) for name in names]


def test_check_confusable_rules() -> None:
    protected = _protected("anthropic")
    assert check_confusable("anthr0pic", protected, owner_key="user:x").rule == "exact"
    assert check_confusable("anthropics", protected, owner_key="user:x").rule == "distance"
    verdict = check_confusable("anthropic-official", protected, owner_key="user:x")
    assert verdict is not None and verdict.blocked and verdict.rule == "affix"
    assert check_confusable("official-anthropic", protected, owner_key="user:x").rule == "affix"
    assert check_confusable("anthropic-mcp", protected, owner_key="user:x").rule == "affix"
    # Distance 2 blocks against a long (>= 8) protected name...
    long_two = check_confusable("anthrxpix", protected, owner_key="user:x")
    assert long_two is not None and long_two.blocked and long_two.rule == "distance"
    # ...and is only a review flag against a shorter one.
    review = check_confusable("kxstrx", _protected("kestra"), owner_key="user:x")
    assert review is not None and not review.blocked and review.rule == "review"
    assert check_confusable("totally-different", protected, owner_key="user:x") is None


def test_i_l_1_fold_applies_to_strong_names_only() -> None:
    strong = _protected("ibm")
    assert check_confusable("1bm", strong, owner_key="user:x").rule == "exact"
    assert check_confusable("lbm", strong, owner_key="user:x").rule == "exact"
    weak = _protected("ibm", strong=False)
    assert check_confusable("1bm", weak, owner_key="user:x") is None


def test_owner_is_exempt_from_its_own_names() -> None:
    protected = _protected("acme-tools", owner="org:acme")
    assert check_confusable("acme-tools-labs", protected, owner_key="org:acme") is None
    assert check_confusable("acme-tools-labs", protected, owner_key="org:other") is not None


def test_short_names_skip_distance_but_not_exact() -> None:
    protected = _protected("acme")
    assert check_confusable("acne", protected, owner_key="user:x") is None  # len < 5
    assert check_confusable("acrne", protected, owner_key="user:x").rule == "exact"


def test_weak_names_get_only_the_exact_check() -> None:
    protected = _protected("widgets", strong=False)
    assert check_confusable("widgetz", protected, owner_key="user:x") is None
    assert check_confusable("w1dgets", protected, owner_key="user:x") is None  # i stays i
    assert check_confusable("widget5", protected, owner_key="user:x").rule == "exact"


def test_reserved_list_and_claim_regex() -> None:
    for name in ("anthropic", "openai", "github", "sessionfs", "personal", "org"):
        assert name in BRAND_RESERVED
    assert NAMESPACE_CLAIM_RE.fullmatch("acme")
    assert NAMESPACE_CLAIM_RE.fullmatch("a-1")
    assert not NAMESPACE_CLAIM_RE.fullmatch("ab")  # too short
    assert not NAMESPACE_CLAIM_RE.fullmatch("a" * 40)  # too long
    assert not NAMESPACE_CLAIM_RE.fullmatch("acme.io")  # no dots
    assert not NAMESPACE_CLAIM_RE.fullmatch("-acme")
    assert not NAMESPACE_CLAIM_RE.fullmatch("аcme")  # Cyrillic


def test_uts39_table_is_pinned_and_complete() -> None:
    from sessionfs.skills.confusables import CONFUSABLES_VERSION, confusables_table

    table = confusables_table()
    assert CONFUSABLES_VERSION == "16.0.0"
    assert len(table) > 6000
    assert table["\u13df"] == "C"  # CHEROKEE LETTER TLI
    assert table["1"] == "l"


@pytest.mark.parametrize(
    "lookalike,target",
    [
        ("ᏟᎻᎪᎢᏀᏢᎢ", "chatgpt"),  # whole-script Cherokee
        ("ch4tgpt", "chatgpt"),
        ("4nthr0pic", "anthropic"),
        ("g00gl3", "google"),
        ("7ermin4l", "terminal"),
        ("ɢoogle", "google"),  # small capital
        ("ANTHROPIC", "anthropic"),
        ("ＡＮＴＨＲＯＰＩＣ", "anthropic"),
    ],
)
def test_full_table_and_leetspeak(lookalike: str, target: str) -> None:
    assert skeleton(lookalike) == skeleton(target)


def test_restriction_levels() -> None:
    from sessionfs.skills.confusables import restriction_level

    assert restriction_level("Acme Tools") == "ascii"
    assert restriction_level("Яндекс") == "single_script"
    assert restriction_level("東京 Labs") == "highly_restrictive"
    assert restriction_level("Acme ไทย") == "moderately_restrictive"
    assert restriction_level("Аcme") == "minimally_restrictive"  # Latin + Cyrillic
    assert restriction_level("Ꭺcme") == "minimally_restrictive"  # Latin + Cherokee
    assert has_mixed_scripts("Gοοgle") and not has_mixed_scripts("東京 Labs")
