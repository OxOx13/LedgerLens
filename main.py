"""LedgerLens CLI — Transaction reconciliation and SQL reporting pipeline.

Usage:
    python main.py
    python main.py --help
    python main.py --list-batches
    python main.py --internal-batch 1 --bank-batch 2
    python main.py --db my_database.db

Milestone 1 behavior:
    Loads CSV files, detects duplicates, reconciles in-memory, exports
    9 files to output/ (including summary.txt).

Milestone 2 behavior:
    Stores transactions in SQLite with integer cents (never REAL).
    Detects repeated imports by SHA-256 hash to prevent double-counting.
    Reconciles batches in SQLite and generates SQL-driven financial reports:
      - Monthly inflows, outflows, and net cash movements (accepted vs excluded)
      - Unmatched transaction counts and signed totals
      - Amount conflicts with internal, bank, and difference amounts
      - Data quality counts (accepted, duplicate, and invalid)
      - Pure SQL algebraic proof verifying "Difference fully explained"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ledgerlens.database import (
    get_batches,
    get_connection,
    get_latest_batch_id,
    import_csv_to_db,
    init_db,
    reconcile_batches_in_db,
)
from ledgerlens.loader import find_duplicates, load_csv
from ledgerlens.reconciler import reconcile
from ledgerlens.reporter import export_all
from ledgerlens.sql_reports import (
    format_sql_reports_text,
    get_amount_conflicts,
    get_data_quality_summary,
    get_monthly_cash_movements,
    get_reconciliation_overview,
    get_unmatched_summary,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LedgerLens: Financial transaction reconciliation & SQL reporting."
    )
    parser.add_argument(
        "--internal",
        default="data/internal_transactions.csv",
        help="Path to internal transactions CSV (default: data/internal_transactions.csv)",
    )
    parser.add_argument(
        "--bank",
        default="data/bank_transactions.csv",
        help="Path to bank transactions CSV (default: data/bank_transactions.csv)",
    )
    parser.add_argument(
        "--db",
        default="ledgerlens.db",
        help="SQLite database file path (default: ledgerlens.db)",
    )
    parser.add_argument(
        "--internal-batch",
        type=int,
        default=None,
        help="Specific internal batch ID to reconcile (defaults to latest)",
    )
    parser.add_argument(
        "--bank-batch",
        type=int,
        default=None,
        help="Specific bank batch ID to reconcile (defaults to latest)",
    )
    parser.add_argument(
        "--list-batches",
        action="store_true",
        help="List all imported batches in the database and exit",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory to save export files (default: output)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    db_path = Path(args.db)

    conn = get_connection(db_path)
    init_db(conn)

    # ------------------------------------------------------------------
    # Option: List batches and exit
    # ------------------------------------------------------------------
    if args.list_batches:
        batches = get_batches(conn)
        print("=" * 60)
        print("  LedgerLens - Imported Batches in SQLite")
        print("=" * 60)
        if not batches:
            print("  No batches have been imported yet.")
        else:
            for b in batches:
                print(
                    f"  Batch #{b['id']}: side={b['dataset_side']} "
                    f"file='{b['source_filename']}' "
                    f"imported_at={b['imported_at']} "
                    f"hash={b['file_hash'][:10]}..."
                )
        print("=" * 60)
        return

    print("=" * 64)
    print("  LedgerLens - Transaction Reconciliation & SQL Reporting")
    print("=" * 64)
    print()

    # ------------------------------------------------------------------
    # Step 1: In-memory loading and milestone 1 export (Preserved)
    # ------------------------------------------------------------------
    print("[1/5] Loading and validating CSV files (Milestone 1)...")
    internal_path = Path(args.internal)
    bank_path = Path(args.bank)

    internal_load = load_csv(internal_path)
    bank_load = load_csv(bank_path)

    print(f"  Internal: {len(internal_load.valid)} valid rows, "
          f"{len(internal_load.errors)} validation errors")
    print(f"  Bank:     {len(bank_load.valid)} valid rows, "
          f"{len(bank_load.errors)} validation errors")

    for label, result in [("Internal", internal_load), ("Bank", bank_load)]:
        for err in result.errors:
            print(f"  [!] {label} row {err.source_row}: {'; '.join(err.errors)}")

    int_unique, int_dupes = find_duplicates(internal_load.valid)
    bank_unique, bank_dupes = find_duplicates(bank_load.valid)

    m1_result = reconcile(int_unique, bank_unique)

    export_all(
        result=m1_result,
        internal_duplicates=int_dupes,
        bank_duplicates=bank_dupes,
        internal_errors=internal_load.errors,
        bank_errors=bank_load.errors,
        output_dir=output_dir,
    )

    # ------------------------------------------------------------------
    # Step 2: Atomic SQLite storage (Idempotent import)
    # ------------------------------------------------------------------
    print()
    print("[2/5] Storing into SQLite (Integer cents, idempotent imports)...")

    int_batch_id, int_is_new = import_csv_to_db(conn, internal_path, "internal")
    int_status = "newly imported" if int_is_new else "already imported (duplicate skipped)"
    print(f"  Internal batch #{int_batch_id}: {int_status}")

    bank_batch_id, bank_is_new = import_csv_to_db(conn, bank_path, "bank")
    bank_status = "newly imported" if bank_is_new else "already imported (duplicate skipped)"
    print(f"  Bank batch     #{bank_batch_id}: {bank_status}")

    # ------------------------------------------------------------------
    # Step 3: Batch Selection
    # ------------------------------------------------------------------
    selected_int_batch = args.internal_batch if args.internal_batch is not None else int_batch_id
    selected_bank_batch = args.bank_batch if args.bank_batch is not None else bank_batch_id

    print()
    print(f"[3/5] Reconciling selected batches: Internal #{selected_int_batch} vs Bank #{selected_bank_batch}...")
    run_id = reconcile_batches_in_db(conn, selected_int_batch, selected_bank_batch)
    print(f"  Created reconciliation run #{run_id} in SQLite")

    # ------------------------------------------------------------------
    # Step 4: SQL Reports
    # ------------------------------------------------------------------
    print()
    print("[4/5] Generating SQL Reports...")
    sql_report_text = format_sql_reports_text(conn, run_id)
    print(sql_report_text)

    # Save SQL report to output directory
    sql_report_path = output_dir / "sql_report.txt"
    sql_report_path.write_text(sql_report_text, encoding="utf-8")
    print(f"\n  -> Saved SQL report to {sql_report_path}")

    # ------------------------------------------------------------------
    # Step 5: Verification (DB results vs Milestone 1 results)
    # ------------------------------------------------------------------
    print()
    print("[5/5] Verifying DB results against Milestone 1 in-memory results...")
    overview = get_reconciliation_overview(conn, run_id)

    assert overview["matched_count"] == len(m1_result.matched), "Matched count mismatch!"
    assert overview["conflict_count"] == len(m1_result.conflicts), "Conflict count mismatch!"
    assert overview["unmatched_internal_count"] == len(m1_result.unmatched_internal), "Unmatched internal mismatch!"
    assert overview["unmatched_bank_count"] == len(m1_result.unmatched_bank), "Unmatched bank mismatch!"
    assert overview["internal_net"] == m1_result.internal_summary.net_movement, "Internal net mismatch!"
    assert overview["bank_net"] == m1_result.bank_summary.net_movement, "Bank net mismatch!"
    assert overview["is_difference_fully_explained"] is True, "Difference is not fully explained!"

    print("  [OK] Exact match count verified: " + str(overview["matched_count"]))
    print("  [OK] Conflict count verified:    " + str(overview["conflict_count"]))
    print("  [OK] Unmatched counts verified:  "
          f"internal={overview['unmatched_internal_count']}, bank={overview['unmatched_bank_count']}")
    print(f"  [OK] Net cash movement matches:  internal={overview['internal_net']}, bank={overview['bank_net']}")
    print("  [OK] SQL algebraic proof:        Difference fully explained = YES")

    print()
    print("Pipeline complete. Milestone 2 SQLite storage and SQL reporting succeeded.")


if __name__ == "__main__":
    main()
