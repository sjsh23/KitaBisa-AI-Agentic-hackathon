"""Checks on generated text before a buyer sees it (FR-SEL-4, D-3, PRD section 10).

The LLM may only repeat numbers it received. `check_explanation` fails the text when:
- it is 120 words or longer, or contains an em or en dash (writing style rule),
- it holds a number or date that is not in the facts it was given,
- it shows a negative Rp amount,
- a LIFNR is glued to a word ("from100108"),
- a required mention is missing (for example "simulated", or "near tie").
The check flags, it never rewrites the text. `assert_masked` stops a prompt that would
send a supplier or buyer name to the model.

`tidy_text` runs before the check on every model text (assessment and explanation,
NFR-5): it fixes spacing ("againts0.23", "stock.Supplier", double spaces) and
misspelled words ("againts" to "against"), and returns the list of fixes for the
report. It only adds or removes spaces and corrects spelling; it never changes a
number, so the grounding check still sees the model's own figures.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# A number may follow letters ("Rp17,550"); thousands use commas, decimals a point.
NUMBER = re.compile(r"(?<![\d.,])\d+(?:,\d{3})*(?:\.\d+)?")
NEGATIVE_AMOUNT = re.compile(r"(?:-|\u2212)\s*Rp\s*\d|Rp\s*(?:-|\u2212)\s*\d", re.IGNORECASE)
DASHES = ("\u2014", "\u2013")   # em dash, en dash
# A lowercase letter that starts the text or a sentence ("... 100109. the recommendation").
SENTENCE_START_LOWER = re.compile(r"(^|[.!?] )([a-z])")
# The same two to four words on both sides of a linking word: "expected cost is expected cost".
# The phrase may not start with an article, so "the risk is the risk cost" is not caught.
REPEATED_PHRASE = re.compile(
    r"\b((?!(?:the|a|an)\b)[a-z]+(?: [a-z]+){1,3}) (?:is|was|are|of|at) (?:an |a |the )?\1\b",
    re.IGNORECASE)
BUYER_ID = re.compile(r"\bBUYER\d+\b")


class MaskingError(RuntimeError):
    """A prompt still contains a supplier or buyer name (PRD section 10)."""


def assert_masked(facts: Any, supplier_names: Iterable[str]) -> None:
    """Raise `MaskingError` if `facts` mention a supplier name or a buyer id."""
    text = json.dumps(facts)
    leaked = [name for name in supplier_names if name and name in text]
    if leaked or BUYER_ID.search(text):
        raise MaskingError(f"masking check failed: {len(leaked)} supplier name(s) or a buyer id "
                           "in the prompt, explanation not requested")


def _scalars(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _scalars(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _scalars(item)
    else:
        yield value


def _numbers_in(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in NUMBER.findall(ISO_DATE.sub(" ", text))]


def grounded_values(facts: Any) -> tuple[set[str], set[float]]:
    """Dates and numbers that appear anywhere in the facts."""
    dates: set[str] = set()
    numbers: set[float] = set()
    for value in _scalars(facts):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            numbers.add(float(value))
        elif isinstance(value, str):
            dates.update(ISO_DATE.findall(value))
            numbers.update(_numbers_in(value))
    return dates, numbers


def glued_lifnrs(text: str, lifnrs: Iterable[str]) -> list[str]:
    """Places where a LIFNR touches a letter, such as "from100108" or "100108is"."""
    found = []
    for lifnr in lifnrs:
        found += re.findall(rf"[A-Za-z]+{lifnr}|{lifnr}[A-Za-z]+", text)
    return found


# ------------------------------------------------------------------ tidy
# A lowercase word glued to a number ("againts0.23", "from100108", "Supplier100108").
# "Rp17,550" is left alone: one lowercase letter only, and the grounding check reads it.
GLUED_WORD_NUMBER = re.compile(r"(?<![\w-])([A-Za-z]*[a-z]{2})(?=\d)")
# A number glued to a word ("100108is", "7,200kg"); ordinals such as "21st" stay.
GLUED_NUMBER_WORD = re.compile(r"(?<=\d)(?!(?:st|nd|rd|th)\b)(?=[a-z]{2,}\b)")
# Sentence or clause punctuation with no space after it ("stock.Supplier", "late,so").
GLUED_SENTENCE = re.compile(r"(?<=[a-z\d%)])([.!?;:])(?=[A-Z])")
GLUED_COMMA = re.compile(r"(?<=[A-Za-z\d%)])([,;])(?=[A-Za-z])")
SPACE_BEFORE_PUNCT = re.compile(r"(?<=\w)[ \t]+(?=[,.;:!?](?:\s|$))")
EXTRA_SPACES = re.compile(r"(?<=\S)[ \t]{2,}")
TRAILING_SPACES = re.compile(r"[ \t]+(?=\n)")
WORD = re.compile(r"\b[A-Za-z]+\b")

# Misspellings seen in model output or common in English text: always corrected.
MISSPELLINGS = {
    "againts": "against", "agianst": "against", "aganist": "against", "agaisnt": "against",
    "recieve": "receive", "recieved": "received", "reciept": "receipt", "reciepts": "receipts",
    "seperate": "separate", "occured": "occurred", "untill": "until", "wich": "which",
    "teh": "the", "adn": "and", "thier": "their", "suplier": "supplier", "supliers": "suppliers",
    "delievery": "delivery", "deliveires": "deliveries", "arival": "arrival",
    "availible": "available", "availabe": "available", "expeceted": "expected",
    "cheapst": "cheapest", "fastst": "fastest", "recomended": "recommended",
    "recommeded": "recommended", "becuase": "because", "beacuse": "because",
}
# Words a GESIT text often uses, on top of the words in the prompt and the facts. A word
# of five letters or more that is one swap of two neighbouring letters away from exactly
# one known word is corrected to it ("suppleir" to "supplier").
COMMON_WORDS = frozenset("""
    about after against already although arrival arrive arrives arriving because before
    below between buyer cheaper cheapest chance cost costs covers deliver delivered
    deliveries delivery downtime expected explain faster fastest figures flagged however
    late later lower order orders other price prices purchase quantity reason recommend
    recommended recommendation remainder risk saving second shortfall simulated since
    still stock supplier suppliers their there these those three through total until
    which while without would
""".split())


def vocabulary(*sources: Any) -> set[str]:
    """Lowercase words in the prompt and facts, plus COMMON_WORDS."""
    words = set(COMMON_WORDS)
    for source in sources:
        text = source if isinstance(source, str) else json.dumps(source)
        words.update(w.lower() for w in WORD.findall(text))
    return words


def _swaps(word: str) -> set[str]:
    return {word[:i] + word[i + 1] + word[i] + word[i + 2:] for i in range(len(word) - 1)}


def correct_word(word: str, known: set[str]) -> str:
    """`word` with its spelling corrected, keeping its capitals, or unchanged."""
    low = word.lower()
    fix = MISSPELLINGS.get(low)
    if fix is None and len(low) >= 5 and low not in known:
        hits = _swaps(low) & known
        fix = hits.pop() if len(hits) == 1 else None
    if fix is None:
        return word
    if word.isupper() and len(word) > 1:
        return fix.upper()
    return fix[0].upper() + fix[1:] if word[0].isupper() else fix


def tidy_text(text: str, *sources: Any) -> tuple[str, list[str]]:
    """Fix spacing, sentence capitals and spelling in a model text; returns the text and
    the fixes made.

    `sources` (system prompt, facts) give the known words for the spelling fix.
    Only spaces are added or removed, sentence starts capitalised and words respelled;
    numbers never change.
    """
    fixes: list[str] = []
    rules = (
        (GLUED_WORD_NUMBER, r"\1 ", "space between word and number"),
        (GLUED_NUMBER_WORD, " ", "space between number and word"),
        (GLUED_SENTENCE, r"\1 ", "space after punctuation"),
        (GLUED_COMMA, r"\1 ", "space after comma"),
        (SPACE_BEFORE_PUNCT, "", "space before punctuation removed"),
        (EXTRA_SPACES, " ", "double space removed"),
        (TRAILING_SPACES, "", "space at line end removed"),
    )
    for pattern, repl, label in rules:
        text, count = pattern.subn(repl, text)
        if count:
            fixes.append(f"{label} x{count}")
    text, count = SENTENCE_START_LOWER.subn(lambda m: m.group(1) + m.group(2).upper(), text)
    if count:
        fixes.append(f"capital letter at sentence start x{count}")
    known = vocabulary(*sources)

    def respell(match: re.Match[str]) -> str:
        word = match.group(0)
        fixed = correct_word(word, known)
        if fixed != word:
            fixes.append(f"spelling {word} -> {fixed}")
        return fixed

    text = WORD.sub(respell, text)
    return text.strip(), fixes


def check_explanation(text: str, facts: Any, max_words: int, lifnrs: Iterable[str] = (),
                      must_mention: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
    """All checks on one explanation. `must_mention` is (label, regex) pairs, case-insensitive."""
    dates, numbers = grounded_values(facts)
    ungrounded = [d for d in ISO_DATE.findall(text) if d not in dates]
    ungrounded += [m for m in NUMBER.findall(ISO_DATE.sub(" ", text))
                   if float(m.replace(",", "")) not in numbers]
    negative = NEGATIVE_AMOUNT.findall(text)
    glued = glued_lifnrs(text, lifnrs)
    missing = [label for label, pattern in must_mention
               if not re.search(pattern, text, re.IGNORECASE)]
    words = len(text.split())
    has_dashes = any(d in text for d in DASHES)
    repeated = repeated_phrases(text)
    return {
        "WORDS": words,
        "WITHIN_WORD_LIMIT": words < max_words,
        "UNGROUNDED_NUMBERS": ungrounded,
        "NEGATIVE_AMOUNTS": negative,
        "MISSING_SPACES": glued,
        "MISSING_MENTIONS": missing,
        "HAS_DASHES": has_dashes,
        "REPEATED_PHRASES": repeated,
        "PASSED": (words < max_words and not ungrounded and not negative and not glued
                   and not missing and not has_dashes and not repeated),
    }


def repeated_phrases(text: str) -> list[str]:
    """Phrases of two or more words said twice around a linking word, such as
    "expected cost is expected cost"."""
    return [m.group(0) for m in REPEATED_PHRASE.finditer(text)]
