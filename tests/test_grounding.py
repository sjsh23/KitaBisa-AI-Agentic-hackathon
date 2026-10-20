"""Checks on generated explanations (FR-SEL-4)."""
import pytest

from agents.grounding import NUMBER, MaskingError, assert_masked, check_explanation, tidy_text

FACTS = {"RECOMMENDED": {"LIFNR": "100109", "EXPECTED_COST": 179397321, "P_LATE_PCT": 29.1,
                         "ARRIVAL": "2026-10-03", "RANK": 1},
         "STOCKOUT_DATE": "2026-10-06", "FLAG": True, "NOTE": None}


def test_grounded_text_passes():
    text = ("Order from supplier 100109 (rank #1): expected cost Rp179,397,321, a 29.1% "
            "chance of delay, arrival 2026-10-03, before the stock-out on 2026-10-06.")
    check = check_explanation(text, FACTS, 120)
    assert check["UNGROUNDED_NUMBERS"] == [] and check["PASSED"] is True


def test_rounded_or_invented_numbers_are_listed():
    text = "Supplier 100109 costs about Rp179.4 million and arrives on 2026-10-02."
    check = check_explanation(text, FACTS, 120)
    assert check["UNGROUNDED_NUMBERS"] == ["2026-10-02", "179.4"]
    assert check["PASSED"] is False


def test_word_limit_and_dashes():
    assert check_explanation("word " * 120, FACTS, 120)["WITHIN_WORD_LIMIT"] is False
    assert check_explanation("word " * 119, FACTS, 120)["WITHIN_WORD_LIMIT"] is True
    check = check_explanation("Supplier 100109 \u2014 best.", FACTS, 120)
    assert check["HAS_DASHES"] is True and check["PASSED"] is False


def test_negative_amount_fails():
    check = check_explanation("Supplier 100109 gives -Rp 1,000,000 saving.", FACTS, 120)
    assert check["NEGATIVE_AMOUNTS"] and check["PASSED"] is False
    assert not check_explanation("A saving of Rp 179,397,321.", FACTS, 120)["NEGATIVE_AMOUNTS"]


def test_lifnr_glued_to_a_word_fails():
    check = check_explanation("Buy from100109 now.", FACTS, 120, lifnrs=["100109"])
    assert check["MISSING_SPACES"] == ["from100109"] and check["PASSED"] is False
    assert check_explanation("Buy from supplier 100109.", FACTS, 120,
                             lifnrs=["100109"])["MISSING_SPACES"] == []


def test_required_mentions():
    must = [("simulated", r"\bsimulated\b"), ("near tie", r"near[ -]tie")]
    check = check_explanation("Supplier 100109 wins. All figures are simulated.", FACTS, 120,
                              must_mention=must)
    assert check["MISSING_MENTIONS"] == ["near tie"] and check["PASSED"] is False


def test_masking_assert():
    assert_masked({"A": "supplier 100109"}, ["PT Sumber Rasa Baru"])
    with pytest.raises(MaskingError):
        assert_masked({"A": "PT Sumber Rasa Baru"}, ["PT Sumber Rasa Baru"])
    with pytest.raises(MaskingError):
        assert_masked({"A": "ordered by BUYER03"}, [])


def test_tidy_fixes_glued_numbers_and_typos():
    raw = ("Supplier100109 has 1.85 days againts0.02 for supplier 100109.It arrives 2026-10-03,"
           "so 7,200kg arrive on the 21st  day , simulated.")
    text, fixes = tidy_text(raw, "1.85 expected days without stock against 0.02")
    assert text == ("Supplier 100109 has 1.85 days against 0.02 for supplier 100109. It arrives "
                    "2026-10-03, so 7,200 kg arrive on the 21st day, simulated.")
    assert "spelling againts -> against" in fixes
    assert any(f.startswith("space between word and number") for f in fixes)


def test_tidy_never_changes_numbers_ids_or_rp():
    raw = "Order 4500000431 (RM-GULA-001, SAP-RPT-1.6, S1) costs Rp17,550 per kg, 29.1% risk."
    text, fixes = tidy_text(raw, {"NETPR": 17550})
    assert text == raw and fixes == []
    assert NUMBER.findall(text) == NUMBER.findall(raw)


def test_tidy_spelling_by_neighbour_swap_uses_known_words_only():
    text, fixes = tidy_text("The suppleir is Late and THIER price is lower.", "supplier")
    assert text == "The supplier is Late and THEIR price is lower."
    assert fixes == ["spelling suppleir -> supplier", "spelling THIER -> THEIR"]
    # A correct word that is not known stays as it is.
    assert tidy_text("A trial order.", "")[0] == "A trial order."


def test_tidied_text_passes_the_glued_lifnr_check():
    text, _ = tidy_text("Buy from100109 now. All figures are simulated.")
    assert text == "Buy from 100109 now. All figures are simulated."
    assert check_explanation(text, FACTS, 120, lifnrs=["100109"])["MISSING_SPACES"] == []


def test_tidy_strips_spaces_at_line_end_and_keeps_paragraphs():
    text, fixes = tidy_text("It is the fastest option. \n\nAll figures are simulated.")
    assert text == "It is the fastest option.\n\nAll figures are simulated."
    assert fixes == ["space at line end removed x1"]


def test_repeated_wording_fails_the_check():
    facts = {"COST": "expected cost Rp 149,600,000"}
    check = check_explanation("Its expected cost is expected cost Rp 149,600,000. Simulated.", facts, 120)
    assert check["REPEATED_PHRASES"] == ["expected cost is expected cost"]
    assert check["PASSED"] is False
    good = check_explanation("Recommend supplier 100109 at an expected cost of Rp 149,600,000. "
                             "The risk is the risk cost of waiting.", facts, 120)
    assert good["REPEATED_PHRASES"] == []


def test_tidy_capitalises_sentence_starts_only():
    text, fixes = tidy_text("It is the fastest option. the recommendation rests on 4 past "
                            "deliveries. Rp 0.27 per kg.")
    assert text == ("It is the fastest option. The recommendation rests on 4 past "
                    "deliveries. Rp 0.27 per kg.")
    assert "capital letter at sentence start x1" in fixes

