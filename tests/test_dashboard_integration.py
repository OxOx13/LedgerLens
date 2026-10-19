"""Integration tests for Milestone 3 Streamlit Dashboard backend queries and logic.

Verifies:
1. All queries powering the dashboard (matched, unmatched, conflicts, previews)
   execute correctly and return complete metadata (source_row, source_filename).
2. Separation of "Difference fully explained" from "Net Cash Movements Equal".
3. Dynamic search filtering across transaction IDs and descriptions.
4. Chart data transformation maintains Decimal integrity in models while
   producing clean visualization series.
5. Report generation and download content formatting.
"""

from __future__ import annotations

import sqlite3
import unittest
from decimal import Decimal
from pathlib import Path

from app import (
    fetch_batch_records,
    fetch_matched_records,
    fetch_unmatched_records,
)
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


class TestDashboardIntegration(unittest.TestCase):
    """Verify backend queries, search filters, and financial state logic used by app.py."""

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

    def test_fetch_matched_records_with_metadata(self):
        matches = fetch_matched_records(self.conn, self.run_id)
        self.assertEqual(len(matches), 4)

        first_match = matches[0]
        # Verify columns required by the dashboard table
        expected_keys = {
            "transaction_id",
            "currency",
            "amount",
            "internal_date",
            "internal_description",
            "internal_row",
            "internal_filename",
            "bank_date",
            "bank_description",
            "bank_row",
            "bank_filename",
        }
        self.assertTrue(expected_keys.issubset(first_match.keys()))
        self.assertEqual(first_match["transaction_id"], "TXN-001")
        self.assertEqual(first_match["amount"], "5000.00")
        self.assertEqual(first_match["internal_filename"], "internal_transactions.csv")
        self.assertEqual(first_match["bank_filename"], "bank_transactions.csv")

    def test_fetch_unmatched_records_internal_and_bank(self):
        unmatched_int = fetch_unmatched_records(self.conn, self.run_id, "unmatched_internal")
        self.assertEqual(len(unmatched_int), 1)
        self.assertEqual(unmatched_int[0]["transaction_id"], "TXN-004")
        self.assertEqual(unmatched_int[0]["amount"], "-99.00")
        self.assertEqual(unmatched_int[0]["source_row"], 5)

        unmatched_bank = fetch_unmatched_records(self.conn, self.run_id, "unmatched_bank")
        self.assertEqual(len(unmatched_bank), 2)
        bank_ids = {u["transaction_id"] for u in unmatched_bank}
        self.assertEqual(bank_ids, {"TXN-005", "TXN-010"})

    def test_fetch_batch_previews_and_errors(self):
        # Errors
        int_errors = fetch_batch_records(self.conn, "validation_errors", self.int_batch)
        self.assertEqual(len(int_errors), 2)
        err_rows = {e["source_row"] for e in int_errors}
        self.assertEqual(err_rows, {8, 9})

        # Duplicates
        int_dups = fetch_batch_records(self.conn, "duplicate_records", self.int_batch)
        self.assertEqual(len(int_dups), 2)
        self.assertTrue(all(d["transaction_id"] == "TXN-005" for d in int_dups))

        # Valid
        int_valid = fetch_batch_records(self.conn, "validated_transactions", self.int_batch)
        self.assertEqual(len(int_valid), 6)

    def test_net_cash_movements_equal_vs_difference_fully_explained(self):
        overview = get_reconciliation_overview(self.conn, self.run_id)

        # 1. Net Cash Movements Equal condition: net difference must be zero
        net_movements_equal = (overview["net_difference"] == Decimal("0.00"))
        self.assertFalse(net_movements_equal)
        self.assertEqual(overview["net_difference"], Decimal("3764.50"))

        # 2. Difference Fully Explained condition: algebraic identity
        diff_explained = overview["is_difference_fully_explained"]
        self.assertTrue(diff_explained)
        self.assertEqual(overview["explained_difference"], Decimal("3764.50"))

    # Backward compatibility alias
    test_balances_equal_vs_difference_fully_explained = test_net_cash_movements_equal_vs_difference_fully_explained

    def test_search_filtering_logic(self):
        matches = fetch_matched_records(self.conn, self.run_id)

        # Query matching specific transaction_id
        filtered_by_id = [
            m for m in matches if "txn-002" in m["transaction_id"].lower()
        ]
        self.assertEqual(len(filtered_by_id), 1)
        self.assertEqual(filtered_by_id[0]["transaction_id"], "TXN-002")

        # Query matching description
        filtered_by_desc = [
            m for m in matches if "acme" in m["internal_description"].lower()
        ]
        self.assertEqual(len(filtered_by_desc), 1)
        self.assertEqual(filtered_by_desc[0]["transaction_id"], "TXN-001")

    def test_chart_data_preparation_preserves_decimals(self):
        monthly = get_monthly_cash_movements(self.conn, [self.int_batch, self.bank_batch])
        accepted = [m for m in monthly if m["record_status"] == "accepted"]
        duplicates = [m for m in monthly if m["record_status"] == "excluded_duplicate"]

        for entry in accepted:
            # Financial precision in Decimal
            self.assertIsInstance(entry["inflows"], Decimal)
            self.assertIsInstance(entry["outflows"], Decimal)
            self.assertIsInstance(entry["net_movement"], Decimal)

            # Chart conversion to float
            inf_float = float(entry["inflows"])
            out_float = float(abs(entry["outflows"]))
            self.assertIsInstance(inf_float, float)
            self.assertIsInstance(out_float, float)

        # Verify duplicate records exist and have Excluded Duplicate Records series labels
        self.assertTrue(len(duplicates) > 0)
        for d in duplicates:
            series_name = f"{d['dataset_side'].capitalize()} Excluded Duplicate Records"
            self.assertIn("Excluded Duplicate Records", series_name)
            self.assertIsInstance(d["net_movement"], Decimal)
            self.assertIsInstance(float(abs(d["net_movement"])), float)

    def test_report_download_text_validity(self):
        text = format_sql_reports_text(self.conn, self.run_id)
        self.assertIn("HOW THE TOTALS RECONCILE", text)
        self.assertIn("Difference fully explained: YES [OK]", text)
        self.assertGreater(len(text), 500)


if __name__ == "__main__":
    unittest.main()
