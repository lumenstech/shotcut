from shotcut.spreadsheet.engine import validate_formula


def test_valid_formula():
    assert validate_formula("=SUM(A1:A10)") == []


def test_missing_equals():
    issues = validate_formula("SUM(A1:A10)")
    assert any(i.severity == "error" for i in issues)


def test_unbalanced_parens():
    issues = validate_formula("=SUM(A1:A10")
    assert any(i.severity == "error" and "arenthes" in i.message for i in issues)


def test_unknown_function_warning():
    issues = validate_formula("=MYFUNC(1, 2)")
    assert any(i.severity == "warning" for i in issues)


def test_string_literal_does_not_confuse_parens():
    # An unbalanced paren *inside* a string literal should not count.
    assert validate_formula('=IF(A1>0, "positive (", "non-positive")') == []
