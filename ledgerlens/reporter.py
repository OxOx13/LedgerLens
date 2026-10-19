"""Export reconciliation results to CSV files and a plain-text summary.

Exported files
--------------
matched.csv             Transactions that agree on ID + currency + amount.
conflicts.csv           Same ID + currency, but amounts disagree.
unmatched_internal.csv  In internal ledger only.
unmatched_bank.csv      In bank statement only.
duplicates_internal.csv Rows with repeated IDs in internal file.
duplicates_bank.csv     Rows with repeated IDs in bank file.
invalid_internal.csv    Rows that failed validation (internal).
invalid_bank.csv        Rows that failed validation (bank).
summary.txt             Human-readable reconciliation narrative.

Design note: we write CSVs with csv.writer (not by hand-joining
strings) so that commas inside descriptions are properly quoted.
"""

from __future__ import annotations

import csv
from collections import Counter
from decimal import Decimal
from pathlib import Path

from .models import Transaction, ValidationError
from .reconciler import (
    CashSummary,
    ConflictPair,
    MatchedPair,
    ReconciliationResult,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def export_all(
    *,
    result: ReconciliationResult,
    internal_duplicates: list[Transaction],
    bank_duplicates: list[Transaction],
    internal_errors: list[ValidationError],
    bank_errors: list[ValidationError],
    output_dir: Path,
) -> None:
    """Write every output file into *output_dir* (created if needed)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    _write_matched(result.matched, output_dir / "matched.csv")
    _write_conflicts(result.conflicts, output_dir / "conflicts.csv")
    _write_transactions(result.unmatched_internal, output_dir / "unmatched_internal.csv")
    _write_transactions(result.unmatched_bank, output_dir / "unmatched_bank.csv")
    _write_transactions(internal_duplicates, output_dir / "duplicates_internal.csv")
    _write_transactions(bank_duplicates, output_dir / "duplicates_bank.csv")
    _write_errors(internal_errors, output_dir / "invalid_internal.csv")
    _write_errors(bank_errors, output_dir / "invalid_bank.csv")
    _write_summary(
        result=result,
        internal_duplicates=internal_duplicates,
        bank_duplicates=bank_duplicates,
        internal_errors=internal_errors,
        bank_errors=bank_errors,
        filepath=output_dir / "summary.txt",
    )


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------

_TXN_HEADER = [
    "transaction_id", "date", "description", "amount", "currency", "source_row",
]


def _write_transactions(txns: list[Transaction], path: Path) -> None:
    """Write a flat list of transactions to CSV."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_TXN_HEADER)
        for t in txns:
            writer.writerow([
                t.transaction_id, t.date.isoformat(), t.description,
                str(t.amount), t.currency, t.source_row,
            ])


def _write_matched(pairs: list[MatchedPair], path: Path) -> None:
    """Side-by-side view: one row per matched pair."""
    header = [
        "transaction_id", "amount", "currency",
        "date_internal", "description_internal", "row_internal",
        "date_bank", "description_bank", "row_bank",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for p in pairs:
            writer.writerow([
                p.internal.transaction_id,
                str(p.internal.amount),
                p.internal.currency,
                p.internal.date.isoformat(),
                p.internal.description,
                p.internal.source_row,
                p.bank.date.isoformat(),
                p.bank.description,
                p.bank.source_row,
            ])


def _write_conflicts(pairs: list[ConflictPair], path: Path) -> None:
    """Highlight the amount disagreement."""
    header = [
        "transaction_id", "currency",
        "amount_internal", "amount_bank", "difference",
        "date_internal", "description_internal", "row_internal",
        "date_bank", "description_bank", "row_bank",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for c in pairs:
            diff = c.bank_amount - c.internal_amount
            writer.writerow([
                c.internal.transaction_id,
                c.internal.currency,
                str(c.internal_amount),
                str(c.bank_amount),
                str(diff),
                c.internal.date.isoformat(),
                c.internal.description,
                c.internal.source_row,
                c.bank.date.isoformat(),
                c.bank.description,
                c.bank.source_row,
            ])


def _write_errors(errors: list[ValidationError], path: Path) -> None:
    """Preserve the raw data and error messages."""
    header = [
        "source_row", "errors",
        "raw_transaction_id", "raw_date", "raw_description",
        "raw_amount", "raw_currency",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for e in errors:
            writer.writerow([
                e.source_row,
                "; ".join(e.errors),
                e.raw_data.get("transaction_id", ""),
                e.raw_data.get("date", ""),
                e.raw_data.get("description", ""),
                e.raw_data.get("amount", ""),
                e.raw_data.get("currency", ""),
            ])


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------


def _write_summary(
    *,
    result: ReconciliationResult,
    internal_duplicates: list[Transaction],
    bank_duplicates: list[Transaction],
    internal_errors: list[ValidationError],
    bank_errors: list[ValidationError],
    filepath: Path,
) -> None:
    """Produce a human-readable reconciliation narrative."""
    lines: list[str] = []
    _a = lines.append  # shorthand

    int_sum = result.internal_summary
    bank_sum = result.bank_summary
    assert int_sum is not None and bank_sum is not None

    # --- Header ---
    _a("=" * 60)
    _a("  LedgerLens — Reconciliation Summary")
    _a("=" * 60)
    _a("")

    # --- Loading stats ---
    int_valid_count = (
        int_sum.transaction_count
        + len(internal_duplicates)
    )
    bank_valid_count = (
        bank_sum.transaction_count
        + len(bank_duplicates)
    )
    int_dup_ids = len({t.transaction_id for t in internal_duplicates})
    bank_dup_ids = len({t.transaction_id for t in bank_duplicates})

    _a("INTERNAL TRANSACTIONS")
    _a(f"  Valid rows loaded:     {int_valid_count}")
    _a(f"  Validation errors:     {len(internal_errors)}")
    _a(f"  Duplicate IDs:         {len(internal_duplicates)} rows ({int_dup_ids} ID{'s' if int_dup_ids != 1 else ''})")
    _a(f"  Unique for matching:   {int_sum.transaction_count}")
    _a("")

    _a("BANK TRANSACTIONS")
    _a(f"  Valid rows loaded:     {bank_valid_count}")
    _a(f"  Validation errors:     {len(bank_errors)}")
    _a(f"  Duplicate IDs:         {len(bank_duplicates)} rows ({bank_dup_ids} ID{'s' if bank_dup_ids != 1 else ''})")
    _a(f"  Unique for matching:   {bank_sum.transaction_count}")
    _a("")

    # --- Reconciliation counts ---
    _a("-" * 60)
    _a("RECONCILIATION RESULTS")
    _a(f"  Exact matches:         {len(result.matched)}")
    _a(f"  Amount conflicts:      {len(result.conflicts)}")
    _a(f"  Unmatched (internal):  {len(result.unmatched_internal)}")
    _a(f"  Unmatched (bank):      {len(result.unmatched_bank)}")
    _a("")

    # --- Cash movements ---
    _a("-" * 60)
    _a("CASH MOVEMENTS — Internal (unique valid)")
    _a(f"  Inflows:       {int_sum.total_inflows:>14} USD")
    _a(f"  Outflows:      {int_sum.total_outflows:>14} USD")
    _a(f"  Net movement:  {int_sum.net_movement:>14} USD")
    _a(f"  Transactions:  {int_sum.transaction_count:>14}")
    _a("")

    _a("CASH MOVEMENTS — Bank (unique valid)")
    _a(f"  Inflows:       {bank_sum.total_inflows:>14} USD")
    _a(f"  Outflows:      {bank_sum.total_outflows:>14} USD")
    _a(f"  Net movement:  {bank_sum.net_movement:>14} USD")
    _a(f"  Transactions:  {bank_sum.transaction_count:>14}")
    _a("")

    # --- Reconciliation explanation ---
    _a("-" * 60)
    _a("HOW THE TOTALS RECONCILE")
    _a("")

    net_diff = bank_sum.net_movement - int_sum.net_movement
    _a(f"  Net difference (bank − internal):  {net_diff} USD")
    _a("")

    # Break down the difference into explainable components.
    #
    # Algebraically:
    #   internal_net = matched_total + conflict_internal_total + unmatched_internal_total
    #   bank_net     = matched_total + conflict_bank_total    + unmatched_bank_total
    #
    #   difference   = (conflict_bank - conflict_internal)
    #                + (unmatched_bank_total - unmatched_internal_total)

    conflict_diff = sum(
        (c.bank_amount - c.internal_amount for c in result.conflicts),
        Decimal("0"),
    )
    unmatched_bank_net = sum(
        (t.amount for t in result.unmatched_bank),
        Decimal("0"),
    )
    unmatched_internal_net = sum(
        (t.amount for t in result.unmatched_internal),
        Decimal("0"),
    )
    explained = conflict_diff + unmatched_bank_net - unmatched_internal_net
    balanced = explained == net_diff

    _a("  This difference is explained by:")
    _a("")
    if result.conflicts:
        _a(f"    Amount conflicts (bank − internal):  {conflict_diff:>+14} USD")
        for c in result.conflicts:
            diff = c.bank_amount - c.internal_amount
            _a(f"      {c.internal.transaction_id}: internal {c.internal_amount}, "
               f"bank {c.bank_amount} (Δ {diff:+})")
    _a(f"    Unmatched bank total:                {unmatched_bank_net:>+14} USD")
    for t in result.unmatched_bank:
        _a(f"      {t.transaction_id}: {t.amount:+}")
    _a(f"    Unmatched internal total:             {unmatched_internal_net:>+14} USD")
    for t in result.unmatched_internal:
        _a(f"      {t.transaction_id}: {t.amount:+}")
    _a("")
    _a(f"    Explained total:                     {explained:>+14} USD")
    _a(f"    Actual difference:                   {net_diff:>+14} USD")
    _a(f"    Difference fully explained: {'YES ✓' if balanced else 'NO ✗  ← investigate!'}")
    _a("")

    # --- Matching assumptions ---
    _a("-" * 60)
    _a("MATCHING ASSUMPTIONS")
    _a("  • Match key: (transaction_id, currency, amount)")
    _a("  • Descriptions ignored (banks routinely rename them)")
    _a("  • Dates ignored (processing delays are common)")
    _a("  • Duplicate IDs excluded from matching (all copies)")
    _a("  • One-to-one matching only")
    _a("  • USD only for this milestone")
    _a("=" * 60)
    _a("")

    filepath.write_text("\n".join(lines), encoding="utf-8")


def build_summary_text(
    *,
    result: ReconciliationResult,
    internal_duplicates: list[Transaction],
    bank_duplicates: list[Transaction],
    internal_errors: list[ValidationError],
    bank_errors: list[ValidationError],
) -> str:
    """Return the summary as a string (for testing without file I/O)."""
    import tempfile

    tmp = Path(tempfile.mktemp(suffix=".txt"))
    try:
        _write_summary(
            result=result,
            internal_duplicates=internal_duplicates,
            bank_duplicates=bank_duplicates,
            internal_errors=internal_errors,
            bank_errors=bank_errors,
            filepath=tmp,
        )
        return tmp.read_text(encoding="utf-8")
    finally:
        tmp.unlink(missing_ok=True)
