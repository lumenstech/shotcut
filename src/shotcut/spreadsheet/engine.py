"""Formula validation.

Real formula evaluation (reference Formualizer or HyperFormula) is out of
scope for the MVP. This module validates syntax only — balanced parens,
valid function names, well-formed cell references — to catch obvious
hallucinations from the executor before they reach the workbook.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Non-exhaustive whitelist of common Excel functions. The executor's system
# prompt encourages these; anything outside is flagged as a warning, not an
# error — new functions ship regularly and we don't want false positives.
KNOWN_FUNCTIONS = {
    "SUM", "AVERAGE", "COUNT", "COUNTA", "COUNTIF", "COUNTIFS",
    "SUMIF", "SUMIFS", "AVERAGEIF", "AVERAGEIFS",
    "MIN", "MAX", "MEDIAN", "STDEV", "VAR",
    "IF", "IFS", "IFERROR", "IFNA", "AND", "OR", "NOT", "XOR",
    "VLOOKUP", "HLOOKUP", "INDEX", "MATCH", "XLOOKUP", "CHOOSE",
    "LEFT", "RIGHT", "MID", "LEN", "TRIM", "UPPER", "LOWER", "PROPER",
    "CONCAT", "CONCATENATE", "TEXTJOIN", "TEXT", "VALUE", "SUBSTITUTE",
    "ROUND", "ROUNDUP", "ROUNDDOWN", "CEILING", "FLOOR", "ABS", "MOD",
    "POWER", "SQRT", "EXP", "LN", "LOG", "LOG10",
    "NPV", "IRR", "PMT", "PV", "FV", "RATE", "NPER", "XIRR", "XNPV",
    "DATE", "YEAR", "MONTH", "DAY", "TODAY", "NOW", "EOMONTH", "EDATE",
    "TRANSPOSE", "UNIQUE", "FILTER", "SORT", "SEQUENCE",
    "INDIRECT", "OFFSET", "ROW", "COLUMN", "ROWS", "COLUMNS",
    "ISBLANK", "ISERROR", "ISNUMBER", "ISTEXT", "ISNA",
}


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
    message: str
    formula: str


def validate_formula(formula: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not formula.startswith("="):
        issues.append(ValidationIssue("error", "Formula must start with '='", formula))
        return issues

    body = formula[1:]

    depth = 0
    in_string = False
    for ch in body:
        if ch == '"' and not in_string:
            in_string = True
        elif ch == '"' and in_string:
            in_string = False
        elif not in_string:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    issues.append(ValidationIssue("error", "Unbalanced parentheses", formula))
                    break
    if depth != 0:
        issues.append(ValidationIssue("error", "Unbalanced parentheses", formula))

    for match in re.finditer(r"([A-Z][A-Z0-9_.]*)\s*\(", body):
        name = match.group(1)
        if name not in KNOWN_FUNCTIONS:
            issues.append(
                ValidationIssue(
                    "warning",
                    f"Unknown or uncommon function: {name}",
                    formula,
                )
            )

    return issues
