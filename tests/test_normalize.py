from splitguard.normalize import (
    FIELD_SEPARATOR,
    combine_text_fields,
    is_empty_content,
    normalize_text,
    redacted_summary,
)


def test_nfkc_casefold_and_whitespace() -> None:
    raw = "  ＡＢＣ\t Straße\n  report   "
    assert normalize_text(raw) == "abc strasse report"


def test_whitespace_only_is_empty() -> None:
    assert normalize_text(" \t\r\n ") == ""
    assert is_empty_content("")


def test_multiple_columns_use_fixed_separator() -> None:
    values = {"a": "  Alpha ", "b": "ＢＥＴＡ", "c": ""}
    assert combine_text_fields(values, ["a", "b", "c"]) == f"alpha{FIELD_SEPARATOR}beta{FIELD_SEPARATOR}"
    assert not is_empty_content(combine_text_fields(values, ["a"]))
    assert is_empty_content(combine_text_fields({"a": " "}, ["a"]))


def test_redacted_summary_never_contains_raw_text() -> None:
    text = "very secret synthetic payload"
    summary = redacted_summary(text)
    assert summary["masked"] == "***"
    assert summary["normalized_char_count"] == len(text)
    assert text not in str(summary)
