"""LedgerLens – Financial transaction reconciliation toolkit.

Milestone 1: CSV loading, validation, exact reconciliation, and
cash-movement reporting.  Standard library only, Decimal arithmetic.

Milestone 2: SQLite database storage (integer cents), atomic transactions,
idempotent batch imports, and SQL reporting.
"""

from .database import (
    cents_to_decimal,
    decimal_to_cents,
    get_connection,
    import_csv_to_db,
    init_db,
    reconcile_batches_in_db,
)
from .loader import find_duplicates, load_csv
from .models import LoadResult, Transaction, ValidationError
from .reconciler import CashSummary, ConflictPair, MatchedPair, ReconciliationResult, reconcile
from .reporter import build_summary_text, export_all
from .sql_reports import (
    get_amount_conflicts,
    get_data_quality_summary,
    get_monthly_cash_movements,
    get_reconciliation_overview,
    get_unmatched_summary,
)

__all__ = [
    "CashSummary",
    "ConflictPair",
    "LoadResult",
    "MatchedPair",
    "ReconciliationResult",
    "Transaction",
    "ValidationError",
    "build_summary_text",
    "cents_to_decimal",
    "decimal_to_cents",
    "export_all",
    "find_duplicates",
    "get_amount_conflicts",
    "get_connection",
    "get_data_quality_summary",
    "get_monthly_cash_movements",
    "get_reconciliation_overview",
    "get_unmatched_summary",
    "import_csv_to_db",
    "init_db",
    "load_csv",
    "reconcile",
    "reconcile_batches_in_db",
]
