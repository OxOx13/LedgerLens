"""SQL reporting functions for LedgerLens Milestone 2.

All reports are implemented via parameterized SQLite queries.
Amounts stored as integer cents in the database are converted to
Decimal for display.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from .database import cents_to_decimal


def get_monthly_cash_movements(
    conn: sqlite3.Connection,
    batch_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Report monthly inflows, outflows, and net cash movement by dataset.

    Clearly separates accepted (validated unique) records from excluded
    (duplicate) records so financial figures are never conflated.

    Parameters
    ----------
    conn : sqlite3.Connection
        Active SQLite connection.
    batch_ids : list[int] | None
        Optional batch IDs to filter by. If None, queries all batches.

    Returns
    -------
    list[dict[str, Any]]
        Aggregated monthly records with Decimal dollar amounts.
    """
    cursor = conn.cursor()
    params: list[Any] = []

    batch_filter_vt = ""
    batch_filter_dr = ""
    if batch_ids:
        placeholders = ",".join("?" for _ in batch_ids)
        batch_filter_vt = f"WHERE batch_id IN ({placeholders})"
        batch_filter_dr = f"WHERE batch_id IN ({placeholders})"
        params = list(batch_ids)

    # 1. Accepted transactions (validated unique)
    query = f"""
    SELECT
        dataset_side,
        SUBSTR(date, 1, 7) AS month,
        'accepted' AS record_status,
        COUNT(*) AS txn_count,
        SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) AS inflows_cents,
        SUM(CASE WHEN amount_cents < 0 THEN amount_cents ELSE 0 END) AS outflows_cents,
        SUM(amount_cents) AS net_cents
    FROM validated_transactions
    {batch_filter_vt}
    GROUP BY dataset_side, SUBSTR(date, 1, 7)

    UNION ALL

    SELECT
        dataset_side,
        SUBSTR(date, 1, 7) AS month,
        'excluded_duplicate' AS record_status,
        COUNT(*) AS txn_count,
        SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) AS inflows_cents,
        SUM(CASE WHEN amount_cents < 0 THEN amount_cents ELSE 0 END) AS outflows_cents,
        SUM(amount_cents) AS net_cents
    FROM duplicate_records
    {batch_filter_dr}
    GROUP BY dataset_side, SUBSTR(date, 1, 7)

    ORDER BY dataset_side ASC, month ASC, record_status ASC;
    """

    # If batch_ids was supplied, we need params twice (for both UNION ALL halves)
    full_params = (params + params) if batch_ids else []
    cursor.execute(query, full_params)
    rows = cursor.fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        results.append({
            "dataset_side": r["dataset_side"],
            "month": r["month"],
            "record_status": r["record_status"],
            "txn_count": int(r["txn_count"]),
            "inflows": cents_to_decimal(r["inflows_cents"] or 0),
            "outflows": cents_to_decimal(r["outflows_cents"] or 0),
            "net_movement": cents_to_decimal(r["net_cents"] or 0),
            "inflows_cents": r["inflows_cents"] or 0,
            "outflows_cents": r["outflows_cents"] or 0,
            "net_cents": r["net_cents"] or 0,
        })
    return results


def get_unmatched_summary(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[dict[str, Any]]:
    """Report unmatched transaction counts and signed totals by dataset."""
    cursor = conn.cursor()
    query = """
    SELECT
        category,
        CASE
            WHEN category = 'unmatched_internal' THEN 'internal'
            WHEN category = 'unmatched_bank' THEN 'bank'
        END AS dataset_side,
        COUNT(*) AS txn_count,
        SUM(
            CASE
                WHEN category = 'unmatched_internal' THEN internal_amount_cents
                WHEN category = 'unmatched_bank' THEN bank_amount_cents
            END
        ) AS total_cents
    FROM reconciliation_results
    WHERE run_id = ? AND category IN ('unmatched_internal', 'unmatched_bank')
    GROUP BY category
    ORDER BY category DESC;
    """
    cursor.execute(query, (run_id,))
    rows = cursor.fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        cents = r["total_cents"] or 0
        results.append({
            "category": r["category"],
            "dataset_side": r["dataset_side"],
            "txn_count": int(r["txn_count"]),
            "total_cents": cents,
            "total_amount": cents_to_decimal(cents),
        })
    return results


def get_amount_conflicts(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[dict[str, Any]]:
    """Report amount conflicts with internal amount, bank amount, and difference.

    Includes source row numbers, descriptions, and dates via SQL JOINs to
    demonstrate tracing back to input files.
    """
    cursor = conn.cursor()
    query = """
    SELECT
        r.transaction_id,
        r.currency,
        r.internal_amount_cents,
        r.bank_amount_cents,
        r.difference_cents,
        it.date AS internal_date,
        it.description AS internal_description,
        it.source_row AS internal_row,
        it.source_filename AS internal_filename,
        bt.date AS bank_date,
        bt.description AS bank_description,
        bt.source_row AS bank_row,
        bt.source_filename AS bank_filename
    FROM reconciliation_results r
    LEFT JOIN validated_transactions it ON r.internal_txn_id = it.id
    LEFT JOIN validated_transactions bt ON r.bank_txn_id = bt.id
    WHERE r.run_id = ? AND r.category = 'conflict'
    ORDER BY r.transaction_id ASC;
    """
    cursor.execute(query, (run_id,))
    rows = cursor.fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        results.append({
            "transaction_id": r["transaction_id"],
            "currency": r["currency"],
            "internal_amount": cents_to_decimal(r["internal_amount_cents"]),
            "bank_amount": cents_to_decimal(r["bank_amount_cents"]),
            "difference": cents_to_decimal(r["difference_cents"]),
            "internal_amount_cents": r["internal_amount_cents"],
            "bank_amount_cents": r["bank_amount_cents"],
            "difference_cents": r["difference_cents"],
            "internal_date": r["internal_date"],
            "internal_description": r["internal_description"],
            "internal_row": r["internal_row"],
            "internal_filename": r["internal_filename"],
            "bank_date": r["bank_date"],
            "bank_description": r["bank_description"],
            "bank_row": r["bank_row"],
            "bank_filename": r["bank_filename"],
        })
    return results


def get_data_quality_summary(
    conn: sqlite3.Connection,
    batch_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Report duplicate and invalid record counts alongside accepted rows."""
    cursor = conn.cursor()
    query = """
    SELECT
        b.id AS batch_id,
        b.dataset_side,
        b.source_filename,
        (SELECT COUNT(*) FROM validated_transactions vt WHERE vt.batch_id = b.id) AS accepted_count,
        (SELECT COUNT(*) FROM duplicate_records dr WHERE dr.batch_id = b.id) AS duplicate_count,
        (SELECT COUNT(DISTINCT transaction_id) FROM duplicate_records dr WHERE dr.batch_id = b.id) AS duplicate_unique_ids,
        (SELECT COUNT(*) FROM validation_errors ve WHERE ve.batch_id = b.id) AS invalid_count
    FROM import_batches b
    """
    params: list[Any] = []
    if batch_ids:
        placeholders = ",".join("?" for _ in batch_ids)
        query += f" WHERE b.id IN ({placeholders})"
        params = list(batch_ids)
    query += " ORDER BY b.id ASC;"

    cursor.execute(query, params)
    rows = cursor.fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        results.append({
            "batch_id": r["batch_id"],
            "dataset_side": r["dataset_side"],
            "source_filename": r["source_filename"],
            "accepted_count": int(r["accepted_count"]),
            "duplicate_count": int(r["duplicate_count"]),
            "duplicate_unique_ids": int(r["duplicate_unique_ids"]),
            "invalid_count": int(r["invalid_count"]),
        })
    return results


def get_reconciliation_overview(
    conn: sqlite3.Connection,
    run_id: int,
) -> dict[str, Any]:
    """Calculate the complete algebraic reconciliation proof purely in SQL."""
    cursor = conn.cursor()

    # Retrieve batch IDs for this run
    cursor.execute(
        "SELECT internal_batch_id, bank_batch_id FROM reconciliation_runs WHERE id = ?",
        (run_id,),
    )
    run_row = cursor.fetchone()
    if run_row is None:
        raise ValueError(f"Reconciliation run {run_id} not found")

    int_batch = run_row["internal_batch_id"]
    bank_batch = run_row["bank_batch_id"]

    # Category counts
    cursor.execute(
        """
        SELECT category, COUNT(*) as cnt
        FROM reconciliation_results
        WHERE run_id = ?
        GROUP BY category
        """,
        (run_id,),
    )
    cat_counts = {r["category"]: int(r["cnt"]) for r in cursor.fetchall()}

    # Cash movement totals for accepted records in these batches
    cursor.execute(
        """
        SELECT
            dataset_side,
            SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) AS inflows,
            SUM(CASE WHEN amount_cents < 0 THEN amount_cents ELSE 0 END) AS outflows,
            SUM(amount_cents) AS net,
            COUNT(*) AS cnt
        FROM validated_transactions
        WHERE batch_id IN (?, ?)
        GROUP BY dataset_side
        """,
        (int_batch, bank_batch),
    )
    cash_by_side = {r["dataset_side"]: r for r in cursor.fetchall()}

    int_cash = cash_by_side.get("internal")
    bank_cash = cash_by_side.get("bank")

    int_net_cents = int_cash["net"] if int_cash else 0
    bank_net_cents = bank_cash["net"] if bank_cash else 0
    net_diff_cents = bank_net_cents - int_net_cents

    # Sum components of difference directly from reconciliation_results
    cursor.execute(
        """
        SELECT
            SUM(CASE WHEN category = 'conflict' THEN difference_cents ELSE 0 END) AS conflict_diff_cents,
            SUM(CASE WHEN category = 'unmatched_bank' THEN bank_amount_cents ELSE 0 END) AS unmatched_bank_cents,
            SUM(CASE WHEN category = 'unmatched_internal' THEN internal_amount_cents ELSE 0 END) AS unmatched_internal_cents
        FROM reconciliation_results
        WHERE run_id = ?
        """,
        (run_id,),
    )
    diff_row = cursor.fetchone()

    conflict_diff_cents = diff_row["conflict_diff_cents"] or 0
    unmatched_bank_cents = diff_row["unmatched_bank_cents"] or 0
    unmatched_internal_cents = diff_row["unmatched_internal_cents"] or 0

    explained_diff_cents = (
        conflict_diff_cents + unmatched_bank_cents - unmatched_internal_cents
    )
    is_fully_explained = (explained_diff_cents == net_diff_cents)

    return {
        "run_id": run_id,
        "internal_batch_id": int_batch,
        "bank_batch_id": bank_batch,
        "matched_count": cat_counts.get("matched", 0),
        "conflict_count": cat_counts.get("conflict", 0),
        "unmatched_internal_count": cat_counts.get("unmatched_internal", 0),
        "unmatched_bank_count": cat_counts.get("unmatched_bank", 0),
        "internal_inflows": cents_to_decimal(int_cash["inflows"] if int_cash else 0),
        "internal_outflows": cents_to_decimal(int_cash["outflows"] if int_cash else 0),
        "internal_net": cents_to_decimal(int_net_cents),
        "internal_txn_count": int_cash["cnt"] if int_cash else 0,
        "bank_inflows": cents_to_decimal(bank_cash["inflows"] if bank_cash else 0),
        "bank_outflows": cents_to_decimal(bank_cash["outflows"] if bank_cash else 0),
        "bank_net": cents_to_decimal(bank_net_cents),
        "bank_txn_count": bank_cash["cnt"] if bank_cash else 0,
        "net_difference": cents_to_decimal(net_diff_cents),
        "conflict_diff": cents_to_decimal(conflict_diff_cents),
        "unmatched_bank_net": cents_to_decimal(unmatched_bank_cents),
        "unmatched_internal_net": cents_to_decimal(unmatched_internal_cents),
        "explained_difference": cents_to_decimal(explained_diff_cents),
        "is_difference_fully_explained": is_fully_explained,
    }


def format_sql_reports_text(conn: sqlite3.Connection, run_id: int) -> str:
    """Format all SQL reports into a clean text narrative."""
    overview = get_reconciliation_overview(conn, run_id)
    quality = get_data_quality_summary(
        conn, [overview["internal_batch_id"], overview["bank_batch_id"]]
    )
    monthly = get_monthly_cash_movements(
        conn, [overview["internal_batch_id"], overview["bank_batch_id"]]
    )
    unmatched = get_unmatched_summary(conn, run_id)
    conflicts = get_amount_conflicts(conn, run_id)

    lines: list[str] = []
    _a = lines.append

    _a("=" * 64)
    _a("  LedgerLens - SQL Reporting & Reconciliation Summary")
    _a("=" * 64)
    _a("")

    # --- Data Quality ---
    _a("DATA QUALITY & RECORD COUNTS BY BATCH")
    _a("-" * 64)
    for q in quality:
        _a(f"  Batch #{q['batch_id']} ({q['dataset_side'].upper()} - {q['source_filename']}):")
        _a(f"    Accepted rows:      {q['accepted_count']:>5}")
        _a(f"    Duplicate rows:     {q['duplicate_count']:>5}  ({q['duplicate_unique_ids']} unique ID{'s' if q['duplicate_unique_ids'] != 1 else ''})")
        _a(f"    Validation errors:  {q['invalid_count']:>5}")
    _a("")

    # --- Reconciliation results ---
    _a("RECONCILIATION RESULTS (Run #{})".format(run_id))
    _a("-" * 64)
    _a(f"  Exact matches:         {overview['matched_count']:>5}")
    _a(f"  Amount conflicts:      {overview['conflict_count']:>5}")
    _a(f"  Unmatched (internal):  {overview['unmatched_internal_count']:>5}")
    _a(f"  Unmatched (bank):      {overview['unmatched_bank_count']:>5}")
    _a("")

    # --- Monthly Cash movements ---
    _a("MONTHLY CASH MOVEMENTS (Accepted vs Excluded)")
    _a("-" * 64)
    _a("  Side      Month     Status               Count     Inflows        Outflows             Net")
    _a("  " + "-" * 76)
    for m in monthly:
        side = m["dataset_side"][:8].ljust(8)
        month = m["month"][:7].ljust(7)
        status = m["record_status"][:18].ljust(18)
        cnt = str(m["txn_count"]).rjust(5)
        inf = str(m["inflows"]).rjust(12)
        out = str(m["outflows"]).rjust(15)
        net = str(m["net_movement"]).rjust(15)
        _a(f"  {side}  {month}  {status} {cnt} {inf} {out} {net}")
    _a("")

    # --- Unmatched Totals ---
    _a("UNMATCHED TRANSACTION TOTALS BY DATASET")
    _a("-" * 64)
    for u in unmatched:
        _a(f"  {u['dataset_side'].capitalize():<10} count: {u['txn_count']:>2},  signed total: {u['total_amount']:>12} USD")
    _a("")

    # --- Amount Conflicts ---
    if conflicts:
        _a("AMOUNT CONFLICTS")
        _a("-" * 64)
        for c in conflicts:
            _a(f"  ID: {c['transaction_id']}")
            _a(f"    Internal (row {c['internal_row']}): {c['internal_amount']:>10} USD")
            _a(f"    Bank     (row {c['bank_row']}): {c['bank_amount']:>10} USD")
            _a(f"    Difference (bank - int): {c['difference']:>+10} USD")
        _a("")

    # --- Difference explanation ---
    _a("HOW THE TOTALS RECONCILE (SQL Algebraic Proof)")
    _a("-" * 64)
    _a(f"  Internal Net Movement:               {overview['internal_net']:>14} USD")
    _a(f"  Bank Net Movement:                   {overview['bank_net']:>14} USD")
    _a(f"  Net Difference (bank - internal):    {overview['net_difference']:>14} USD")
    _a("")
    _a("  Difference components:")
    _a(f"    Amount conflicts (bank - internal):{overview['conflict_diff']:>14} USD")
    _a(f"    Unmatched bank net total:          {overview['unmatched_bank_net']:>14} USD")
    _a(f"    Unmatched internal net total:      {overview['unmatched_internal_net']:>14} USD")
    _a(f"    Explained Total:                   {overview['explained_difference']:>14} USD")
    status_str = "YES [OK]" if overview["is_difference_fully_explained"] else "NO [!] <- investigate"
    _a(f"    Difference fully explained: {status_str}")
    _a("=" * 64)

    return "\n".join(lines)
