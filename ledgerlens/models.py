"""Data classes shared across all LedgerLens modules.

Design notes
------------
Why dataclasses instead of plain dicts?
  • Type hints make every field's purpose obvious.
  • IDEs can autocomplete field names, catching typos early.
  • The frozen=False default lets us build objects incrementally
    during CSV parsing while still being readable.

Why Decimal for money?
  float(0.1 + 0.2) == 0.30000000000000004.  Financial arithmetic
  must be exact, so we use decimal.Decimal throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Transaction:
    """A single validated financial transaction.

    Attributes
    ----------
    transaction_id : str
        Unique-ish identifier (may have duplicates — that's a data
        quality issue we detect, not prevent).
    date : date
        Transaction date parsed from ISO-8601 (YYYY-MM-DD).
    description : str
        Free-text memo.  Deliberately excluded from matching because
        banks routinely truncate or rephrase descriptions.
    amount : Decimal
        Signed value: positive = inflow (money in),
        negative = outflow (money out).
    currency : str
        ISO-4217 code.  USD only for milestone 1.
    source_row : int
        1-based CSV row number (row 1 = header, row 2 = first data
        row).  Preserved so error messages point back to the file.
    """

    transaction_id: str
    date: date
    description: str
    amount: Decimal
    currency: str
    source_row: int


@dataclass
class ValidationError:
    """A CSV row that failed validation.

    We keep the raw data so the user can see exactly what was wrong
    and fix the source file.
    """

    source_row: int
    raw_data: dict[str, str]
    errors: list[str] = field(default_factory=list)


@dataclass
class LoadResult:
    """Everything returned by loading a single CSV file.

    Separating valid rows from errors at load time means downstream
    code never needs to re-check validity.
    """

    valid: list[Transaction] = field(default_factory=list)
    errors: list[ValidationError] = field(default_factory=list)
    source_file: str = ""
