"""CSV loading, row-level validation, and duplicate detection.

Processing pipeline
-------------------
1. Open the CSV and verify the header has all required columns.
2. For each data row, validate every field independently.
   Valid rows become Transaction objects; invalid rows become
   ValidationError objects with human-readable error messages.
3. After loading, call find_duplicates() to separate rows whose
   transaction_id appears more than once within the same file.

Why validate row-by-row?
  A single bad row shouldn't prevent us from processing the other
  999 good rows.  Row-level errors also produce better diagnostics
  ("row 7: invalid date '2026-31-03'") than file-level failures.

Why detect duplicates as a separate step?
  Loading and validation are about *individual row correctness*.
  Duplicate detection is about *cross-row relationships*.  Keeping
  them separate makes each function easier to test and reason about.
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import LoadResult, Transaction, ValidationError

# The five columns every transaction CSV must contain.
EXPECTED_COLUMNS = frozenset(
    {"transaction_id", "date", "description", "amount", "currency"}
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_csv(filepath: str | Path) -> LoadResult:
    """Load a transaction CSV, returning validated rows and errors.

    Parameters
    ----------
    filepath : str or Path
        Path to a CSV with columns: transaction_id, date,
        description, amount, currency.

    Returns
    -------
    LoadResult
        .valid  — list of Transaction (all fields verified)
        .errors — list of ValidationError (with row numbers)

    The function never raises on bad data; it collects errors instead.
    It *will* raise FileNotFoundError / PermissionError for OS-level
    problems — those aren't data-quality issues.
    """
    filepath = Path(filepath)
    result = LoadResult(source_file=str(filepath))

    with filepath.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)

        # --- Header validation ---
        if reader.fieldnames is None:
            result.errors.append(
                ValidationError(
                    source_row=1,
                    raw_data={},
                    errors=["File is empty or has no header row"],
                )
            )
            return result

        # Strip whitespace from column names (common CSV quirk).
        cleaned_names = [name.strip() for name in reader.fieldnames]
        missing = EXPECTED_COLUMNS - set(cleaned_names)
        if missing:
            result.errors.append(
                ValidationError(
                    source_row=1,
                    raw_data={"headers": str(reader.fieldnames)},
                    errors=[f"Missing required columns: {sorted(missing)}"],
                )
            )
            return result

        # --- Row-by-row validation ---
        # enumerate starts at 2 because row 1 is the header.
        for row_index, raw_row in enumerate(reader, start=2):
            # Normalise whitespace in both keys and values.
            row = {
                k.strip(): (v.strip() if v else "")
                for k, v in raw_row.items()
                if k is not None
            }

            errors = _validate_row(row)

            if errors:
                result.errors.append(
                    ValidationError(
                        source_row=row_index,
                        raw_data=dict(row),
                        errors=errors,
                    )
                )
            else:
                result.valid.append(
                    Transaction(
                        transaction_id=row["transaction_id"],
                        date=_parse_date(row["date"]),  # type: ignore[arg-type]
                        description=row["description"],
                        amount=Decimal(row["amount"]),
                        currency=row["currency"].upper(),
                        source_row=row_index,
                    )
                )

    return result


def find_duplicates(
    transactions: list[Transaction],
) -> tuple[list[Transaction], list[Transaction]]:
    """Split transactions into unique and duplicate groups.

    If a transaction_id appears N > 1 times, *all N rows* go into the
    duplicates list.  This is deliberate: we cannot know which copy is
    the "real" one, so none of them should participate in
    reconciliation.

    Returns
    -------
    (unique, duplicates)
        unique     — transactions whose ID appears exactly once
        duplicates — transactions whose ID appears more than once
    """
    id_counts: Counter[str] = Counter(t.transaction_id for t in transactions)
    dup_ids = {tid for tid, count in id_counts.items() if count > 1}

    unique = [t for t in transactions if t.transaction_id not in dup_ids]
    duplicates = [t for t in transactions if t.transaction_id in dup_ids]

    return unique, duplicates


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_row(row: dict[str, str]) -> list[str]:
    """Check one CSV row for problems.  Returns a list of error strings
    (empty list = valid).

    Each field is validated independently so the user sees *all*
    problems at once, not just the first one.
    """
    errors: list[str] = []

    # transaction_id — must be non-empty
    if not row.get("transaction_id"):
        errors.append("Missing transaction_id")

    # date — must parse as YYYY-MM-DD
    date_str = row.get("date", "")
    if not date_str:
        errors.append("Missing date")
    elif _parse_date(date_str) is None:
        errors.append(f"Invalid date: '{date_str}'")

    # amount — must parse as Decimal (not float!)
    amount_str = row.get("amount", "")
    if not amount_str:
        errors.append("Missing amount")
    else:
        try:
            amt = Decimal(amount_str)
            if not amt.is_finite():
                errors.append(f"Invalid amount (non-finite): '{amount_str}'")
            else:
                cents = amt * 100
                if cents != cents.to_integral_value():
                    errors.append(f"Invalid amount (fractional cents not supported): '{amount_str}'")
        except InvalidOperation:
            errors.append(f"Invalid amount: '{amount_str}'")

    # currency — must be non-empty
    if not row.get("currency"):
        errors.append("Missing currency")

    return errors


def _parse_date(date_str: str):
    """Parse a YYYY-MM-DD string into a datetime.date.

    Returns None on failure instead of raising, because we collect
    errors rather than aborting on the first bad row.

    Why strptime and not date.fromisoformat?
      date.fromisoformat is fine too, but strptime gives us explicit
      control over the expected format and works identically across
      Python 3.7+.  (fromisoformat gained full ISO-8601 support only
      in Python 3.11.)
    """
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
