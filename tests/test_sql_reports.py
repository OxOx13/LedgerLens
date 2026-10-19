"""Tests for SQL reports and reconciliation proof.

Verifies:
1. Monthly cash movements distinguish accepted records from excluded duplicates.
2. Unmatched transaction counts and signed totals by dataset side.
3. Amount conflicts detail internal amount, bank amount, difference, and source rows.
4. Data quality summaries count accepted, duplicate, and invalid records.
5. All SQL totals match hand-calculated expected financial values.
"""

from __future__ import annotations

import sqlite3
import unittest
from decimal import Decimal
from pathlib import Path

from ledgerlens.database import (
    get_connection,
    import_csv_to_db,
    init_db,
    reconcile_batches_in_db,
)
from ledgerlens.sql_reports import (
    format_sql_reports_text,
    get_amount_conflicts,
    get_data_quality_summary,
    get_monthly_cash_movements,
    get_reconciliation_overview,
    get_unmatched_summary,
)


class TestSQLReports(unittest.TestCase):
    """Test SQL aggregation queries on the standard sample datasets."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

        int_csv = Path("data/internal_transactions.csv")
        bank_csv = Path("data/bank_transactions.csv")

        self.int_batch, _ = import_csv_to_db(self.conn, int_csv, "internal")
        self.bank_batch, _ = import_csv_to_db(self.conn, bank_csv, "bank")
        self.run_id = reconcile_batches_in_db(self.conn, self.int_batch, self.bank_batch)

    def tearDown(self):
        self.conn.close()

    def test_monthly_cash_movements_accepted_vs_excluded(self):
        movements = get_monthly_cash_movements(self.conn, [self.int_batch, self.bank_batch])

        # Expect 4 entries: internal accepted, internal excluded, bank accepted, bank excluded
        self.assertEqual(len(movements), 4)

        by_side_status = {(m["dataset_side"], m["record_status"]): m for m in movements}

        # Internal Accepted
        int_acc = by_side_status[("internal", "accepted")]
        self.assertEqual(int_acc["month"], "2026-03")
        self.assertEqual(int_acc["txn_count"], 6)
        self.assertEqual(int_acc["inflows"], Decimal("17042.17"))
        self.assertEqual(int_acc["outflows"], Decimal("-1098.99"))
        self.assertEqual(int_acc["net_movement"], Decimal("15943.18"))

        # Internal Excluded (duplicates)
        int_exc = by_side_status[("internal", "excluded_duplicate")]
        self.assertEqual(int_exc["month"], "2026-03")
        self.assertEqual(int_exc["txn_count"], 2)
        self.assertEqual(int_exc["net_movement"], Decimal("4700.50"))

        # Bank Accepted
        bank_acc = by_side_status[("bank", "accepted")]
        self.assertEqual(bank_acc["month"], "2026-03")
        self.assertEqual(bank_acc["txn_count"], 7)
        self.assertEqual(bank_acc["inflows"], Decimal("20742.67"))
        self.assertEqual(bank_acc["outflows"], Decimal("-1034.99"))
        self.assertEqual(bank_acc["net_movement"], Decimal("19707.68"))

        # Bank Excluded (duplicates)
        bank_exc = by_side_status[("bank", "excluded_duplicate")]
        self.assertEqual(bank_exc["month"], "2026-03")
        self.assertEqual(bank_exc["txn_count"], 2)
        self.assertEqual(bank_exc["net_movement"], Decimal("1000.00"))

    def test_unmatched_summary(self):
        unmatched = get_unmatched_summary(self.conn, self.run_id)
        self.assertEqual(len(unmatched), 2)

        by_side = {u["dataset_side"]: u for u in unmatched}

        # Unmatched internal
        self.assertIn("internal", by_side)
        self.assertEqual(by_side["internal"]["txn_count"], 1)
        self.assertEqual(by_side["internal"]["total_amount"], Decimal("-99.00"))

        # Unmatched bank
        self.assertIn("bank", by_side)
        self.assertEqual(by_side["bank"]["txn_count"], 2)
        self.assertEqual(by_side["bank"]["total_amount"], Decimal("3165.50"))

    def test_amount_conflicts(self):
        conflicts = get_amount_conflicts(self.conn, self.run_id)
        self.assertEqual(len(conflicts), 1)

        c = conflicts[0]
        self.assertEqual(c["transaction_id"], "TXN-003")
        self.assertEqual(c["currency"], "USD")
        self.assertEqual(c["internal_amount"], Decimal("12000.00"))
        self.assertEqual(c["bank_amount"], Decimal("12500.00"))
        self.assertEqual(c["difference"], Decimal("500.00"))
        # Verify row tracing
        self.assertEqual(c["internal_row"], 4)
        self.assertEqual(c["bank_row"], 4)

    def test_data_quality_summary(self):
        quality = get_data_quality_summary(self.conn, [self.int_batch, self.bank_batch])
        self.assertEqual(len(quality), 2)

        by_side = {q["dataset_side"]: q for q in quality}

        # Internal
        self.assertEqual(by_side["internal"]["accepted_count"], 6)
        self.assertEqual(by_side["internal"]["duplicate_count"], 2)
        self.assertEqual(by_side["internal"]["duplicate_unique_ids"], 1)
        self.assertEqual(by_side["internal"]["invalid_count"], 2)

        # Bank
        self.assertEqual(by_side["bank"]["accepted_count"], 7)
        self.assertEqual(by_side["bank"]["duplicate_count"], 2)
        self.assertEqual(by_side["bank"]["duplicate_unique_ids"], 1)
        self.assertEqual(by_side["bank"]["invalid_count"], 2)

    def test_format_sql_reports_text(self):
        text = format_sql_reports_text(self.conn, self.run_id)
        self.assertIn("MONTHLY CASH MOVEMENTS (Accepted vs Excluded)", text)
        self.assertIn("UNMATCHED TRANSACTION TOTALS BY DATASET", text)
        self.assertIn("AMOUNT CONFLICTS", text)
        self.assertIn("HOW THE TOTALS RECONCILE (SQL Algebraic Proof)", text)
        self.assertIn("Difference fully explained: YES [OK]", text)


if __name__ == "__main__":
    unittest.main()
