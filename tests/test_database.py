"""Tests for SQLite database storage, schema constraints, and atomic transactions.

Verifies:
1. Integer cents conversion, finite validation, rejection of fractional cents.
2. Idempotent imports (detecting identical file by SHA-256 hash).
3. Atomic transaction rollback (failed imports leave no partial data).
4. Traceability of every record back to source file, row, and batch ID.
5. Batch selection for reconciliation.
6. DB reconciliation results matching Milestone 1 hand-calculated values.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ledgerlens.database import (
    cents_to_decimal,
    decimal_to_cents,
    get_connection,
    init_db,
    import_csv_to_db,
    reconcile_batches_in_db,
)
from ledgerlens.sql_reports import get_reconciliation_overview


class TestIntegerCents(unittest.TestCase):
    """Test integer cents conversion and strict validation."""

    def test_valid_cents_conversion(self):
        self.assertEqual(decimal_to_cents(Decimal("100.00")), 10000)
        self.assertEqual(decimal_to_cents(Decimal("-50.25")), -5025)
        self.assertEqual(decimal_to_cents(Decimal("0.00")), 0)
        self.assertEqual(decimal_to_cents(Decimal("0")), 0)
        self.assertEqual(decimal_to_cents(Decimal("12345.67")), 1234567)

    def test_cents_to_decimal(self):
        self.assertEqual(cents_to_decimal(10000), Decimal("100.00"))
        self.assertEqual(cents_to_decimal(-5025), Decimal("-50.25"))
        self.assertEqual(cents_to_decimal(0), Decimal("0.00"))
        self.assertEqual(cents_to_decimal(1234567), Decimal("12345.67"))

    def test_reject_fractional_cents(self):
        """Fractional cents must be rejected, never silently rounded."""
        with self.assertRaises(ValueError):
            decimal_to_cents(Decimal("10.005"))

        with self.assertRaises(ValueError):
            decimal_to_cents(Decimal("0.001"))

        with self.assertRaises(ValueError):
            decimal_to_cents(Decimal("-99.999"))

    def test_reject_non_finite_values(self):
        """NaN and Infinities must be rejected."""
        with self.assertRaises(ValueError):
            decimal_to_cents(Decimal("NaN"))

        with self.assertRaises(ValueError):
            decimal_to_cents(Decimal("Infinity"))

        with self.assertRaises(ValueError):
            decimal_to_cents(Decimal("-Infinity"))

    def test_reject_non_decimal_type(self):
        with self.assertRaises(TypeError):
            decimal_to_cents(10.50)  # type: ignore[arg-type]

        with self.assertRaises(TypeError):
            cents_to_decimal("1000")  # type: ignore[arg-type]


class TestDatabaseSchema(unittest.TestCase):
    """Verify tables, columns, foreign keys, and integer data types."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_amount_columns_are_integer_never_real(self):
        cursor = self.conn.cursor()
        for table in ("validated_transactions", "duplicate_records", "reconciliation_results"):
            cursor.execute(f"PRAGMA table_info({table})")
            columns = {row["name"]: row["type"] for row in cursor.fetchall()}
            for col_name, col_type in columns.items():
                if "amount" in col_name or "cents" in col_name:
                    self.assertEqual(
                        col_type.upper(),
                        "INTEGER",
                        f"Column {col_name} in {table} should be INTEGER, found {col_type}",
                    )

    def test_foreign_keys_enabled(self):
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA foreign_keys")
        enabled = cursor.fetchone()[0]
        self.assertEqual(enabled, 1)


class TestIdempotencyAndAtomicImports(unittest.TestCase):
    """Verify repeated imports are safe and failed imports leave no partial data."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

        self.valid_csv = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,2026-01-10,Sale,1000.00,USD\n"
            "T-002,2026-01-11,Fee,-50.00,USD\n"
        )
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        )
        self.tmp.write(self.valid_csv)
        self.tmp.close()

    def tearDown(self):
        self.conn.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_repeated_import_does_not_duplicate_records(self):
        # First import
        batch_id_1, is_new_1 = import_csv_to_db(self.conn, self.tmp.name, "internal")
        self.assertTrue(is_new_1)

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM validated_transactions WHERE batch_id = ?", (batch_id_1,))
        count_1 = cursor.fetchone()[0]
        self.assertEqual(count_1, 2)

        # Second import of identical file
        batch_id_2, is_new_2 = import_csv_to_db(self.conn, self.tmp.name, "internal")
        self.assertFalse(is_new_2)
        self.assertEqual(batch_id_1, batch_id_2)

        # Ensure table rows did not double
        cursor.execute("SELECT COUNT(*) FROM validated_transactions")
        total_count = cursor.fetchone()[0]
        self.assertEqual(total_count, 2)

        cursor.execute("SELECT COUNT(*) FROM import_batches")
        batch_count = cursor.fetchone()[0]
        self.assertEqual(batch_count, 1)

    def test_fractional_cents_recorded_as_validation_error_while_valid_rows_continue(self):
        """Invalid individual rows, including fractional cents, must be recorded
        as validation errors while valid rows continue importing."""
        mixed_csv = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,2026-01-10,Valid1,100.00,USD\n"
            "T-002,2026-01-11,FractionalCents,50.005,USD\n"
            "T-003,2026-01-12,Valid2,-25.50,USD\n"
            "T-004,2026-02-30,BadDate,75.00,USD\n"
        )
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        )
        tmp.write(mixed_csv)
        tmp.close()

        try:
            batch_id, is_new = import_csv_to_db(self.conn, tmp.name, "internal")
            self.assertTrue(is_new)

            cursor = self.conn.cursor()
            # Valid rows (T-001 and T-003) should be imported
            cursor.execute(
                "SELECT transaction_id, amount_cents FROM validated_transactions WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            )
            val_rows = cursor.fetchall()
            self.assertEqual(len(val_rows), 2)
            self.assertEqual(val_rows[0]["transaction_id"], "T-001")
            self.assertEqual(val_rows[0]["amount_cents"], 10000)
            self.assertEqual(val_rows[1]["transaction_id"], "T-003")
            self.assertEqual(val_rows[1]["amount_cents"], -2550)

            # Invalid rows (T-002 fractional cents and T-004 bad date) should be in validation_errors
            cursor.execute(
                "SELECT source_row, errors FROM validation_errors WHERE batch_id = ? ORDER BY source_row",
                (batch_id,),
            )
            err_rows = cursor.fetchall()
            self.assertEqual(len(err_rows), 2)
            self.assertEqual(err_rows[0]["source_row"], 3)
            self.assertIn("fractional cents", err_rows[0]["errors"])
            self.assertEqual(err_rows[1]["source_row"], 5)
            self.assertIn("Invalid date", err_rows[1]["errors"])
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_database_failure_rolls_back_entire_import_atomically(self):
        """A database-level failure must roll back the entire import atomically,
        leaving no partial batch or transaction records."""
        valid_csv = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,2026-01-10,Valid1,100.00,USD\n"
            "T-002,2026-01-11,Valid2,200.00,USD\n"
        )
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        )
        tmp.write(valid_csv)
        tmp.close()

        try:
            # Install an SQLite trigger that simulates a database failure during transaction insert
            self.conn.execute(
                "CREATE TRIGGER sim_db_failure BEFORE INSERT ON validated_transactions "
                "BEGIN SELECT RAISE(ABORT, 'Simulated DB failure during batch insert'); END;"
            )

            with self.assertRaises(sqlite3.DatabaseError):
                import_csv_to_db(self.conn, tmp.name, "internal")

            # Check database is completely empty: rollback succeeded
            cursor = self.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM import_batches")
            self.assertEqual(cursor.fetchone()[0], 0)

            cursor.execute("SELECT COUNT(*) FROM validated_transactions")
            self.assertEqual(cursor.fetchone()[0], 0)

            cursor.execute("SELECT COUNT(*) FROM validation_errors")
            self.assertEqual(cursor.fetchone()[0], 0)
        finally:
            self.conn.execute("DROP TRIGGER IF EXISTS sim_db_failure")
            Path(tmp.name).unlink(missing_ok=True)


class TestTraceabilityAndBatchSelection(unittest.TestCase):
    """Verify tracing records to input files and selecting batches for reconciliation."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

        # Create two internal CSVs and one bank CSV
        self.csv_int1 = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,2026-01-10,Batch1,100.00,USD\n"
        )
        self.csv_int2 = (
            "transaction_id,date,description,amount,currency\n"
            "T-002,2026-01-15,Batch2,200.00,USD\n"
        )
        self.csv_bank = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,2026-01-10,Batch1Match,100.00,USD\n"
        )

        self.f_int1 = self._write_tmp(self.csv_int1)
        self.f_int2 = self._write_tmp(self.csv_int2)
        self.f_bank = self._write_tmp(self.csv_bank)

    def tearDown(self):
        self.conn.close()
        for p in (self.f_int1, self.f_int2, self.f_bank):
            Path(p).unlink(missing_ok=True)

    def _write_tmp(self, text: str) -> str:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        )
        f.write(text)
        f.close()
        return f.name

    def test_traceability_via_foreign_keys(self):
        b1, _ = import_csv_to_db(self.conn, self.f_int1, "internal")
        b2, _ = import_csv_to_db(self.conn, self.f_int2, "internal")
        b_bank, _ = import_csv_to_db(self.conn, self.f_bank, "bank")

        # Selectively reconcile batch 1 with bank batch
        run_id = reconcile_batches_in_db(self.conn, internal_batch_id=b1, bank_batch_id=b_bank)

        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT
                r.category,
                r.transaction_id,
                it.source_filename AS int_filename,
                it.source_row AS int_row,
                it.batch_id AS int_batch,
                bt.source_filename AS bank_filename,
                bt.source_row AS bank_row,
                bt.batch_id AS bank_batch
            FROM reconciliation_results r
            LEFT JOIN validated_transactions it ON r.internal_txn_id = it.id
            LEFT JOIN validated_transactions bt ON r.bank_txn_id = bt.id
            WHERE r.run_id = ?
            """,
            (run_id,),
        )
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["category"], "matched")
        self.assertEqual(r["transaction_id"], "T-001")
        self.assertEqual(r["int_batch"], b1)
        self.assertEqual(r["int_row"], 2)
        self.assertEqual(r["bank_batch"], b_bank)
        self.assertEqual(r["bank_row"], 2)


class TestReconciliationMatchesMilestone1(unittest.TestCase):
    """End-to-end verification that SQLite storage matches Milestone 1 figures."""

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

    def test_reconciliation_exact_figures(self):
        overview = get_reconciliation_overview(self.conn, self.run_id)

        # Exact counts
        self.assertEqual(overview["matched_count"], 4)
        self.assertEqual(overview["conflict_count"], 1)
        self.assertEqual(overview["unmatched_internal_count"], 1)
        self.assertEqual(overview["unmatched_bank_count"], 2)

        # Cash movements
        self.assertEqual(overview["internal_inflows"], Decimal("17042.17"))
        self.assertEqual(overview["internal_outflows"], Decimal("-1098.99"))
        self.assertEqual(overview["internal_net"], Decimal("15943.18"))
        self.assertEqual(overview["internal_txn_count"], 6)

        self.assertEqual(overview["bank_inflows"], Decimal("20742.67"))
        self.assertEqual(overview["bank_outflows"], Decimal("-1034.99"))
        self.assertEqual(overview["bank_net"], Decimal("19707.68"))
        self.assertEqual(overview["bank_txn_count"], 7)

        # Difference
        self.assertEqual(overview["net_difference"], Decimal("3764.50"))
        self.assertEqual(overview["conflict_diff"], Decimal("500.00"))
        self.assertEqual(overview["unmatched_bank_net"], Decimal("3165.50"))
        self.assertEqual(overview["unmatched_internal_net"], Decimal("-99.00"))
        self.assertEqual(overview["explained_difference"], Decimal("3764.50"))
        self.assertTrue(overview["is_difference_fully_explained"])


if __name__ == "__main__":
    unittest.main()
