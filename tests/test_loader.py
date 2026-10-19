"""Tests for the CSV loader and duplicate detection.

Every test uses tiny inline CSV data so the expected results are
obvious and hand-verifiable.  We write temporary files with
tempfile so tests don't depend on the data/ directory.
"""

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from ledgerlens.loader import find_duplicates, load_csv


class TestLoadValidCSV(unittest.TestCase):
    """Happy path: a small, perfectly valid CSV."""

    def setUp(self):
        self.csv_text = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,2026-01-10,Sale,1000.00,USD\n"
            "T-002,2026-01-11,Rent,-500.00,USD\n"
        )
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8",
        )
        self.tmp.write(self.csv_text)
        self.tmp.close()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_all_rows_are_valid(self):
        result = load_csv(self.tmp.name)
        self.assertEqual(len(result.valid), 2)
        self.assertEqual(len(result.errors), 0)

    def test_first_row_fields(self):
        result = load_csv(self.tmp.name)
        txn = result.valid[0]
        self.assertEqual(txn.transaction_id, "T-001")
        self.assertEqual(txn.date, date(2026, 1, 10))
        self.assertEqual(txn.description, "Sale")
        self.assertEqual(txn.amount, Decimal("1000.00"))
        self.assertEqual(txn.currency, "USD")
        self.assertEqual(txn.source_row, 2)  # row 1 = header

    def test_negative_amount_preserved(self):
        """Outflows must stay negative — never silently abs()'d."""
        result = load_csv(self.tmp.name)
        self.assertEqual(result.valid[1].amount, Decimal("-500.00"))

    def test_amount_is_decimal_not_float(self):
        """Decimal prevents floating-point rounding errors."""
        result = load_csv(self.tmp.name)
        self.assertIsInstance(result.valid[0].amount, Decimal)


class TestInvalidDate(unittest.TestCase):
    """Rows with unparseable dates go to errors, not valid."""

    def test_bad_month(self):
        csv_text = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,2026-13-01,Bad month,100.00,USD\n"
        )
        tmp = self._write(csv_text)
        result = load_csv(tmp)
        self.assertEqual(len(result.valid), 0)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("Invalid date", result.errors[0].errors[0])
        Path(tmp).unlink()

    def test_impossible_day(self):
        """Feb 30 is not a real date."""
        csv_text = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,2026-02-30,Feb 30,100.00,USD\n"
        )
        tmp = self._write(csv_text)
        result = load_csv(tmp)
        self.assertEqual(len(result.valid), 0)
        self.assertEqual(len(result.errors), 1)
        Path(tmp).unlink()

    def test_reversed_format(self):
        """DD-MM-YYYY should not parse as YYYY-MM-DD."""
        csv_text = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,31-03-2026,Reversed,100.00,USD\n"
        )
        tmp = self._write(csv_text)
        result = load_csv(tmp)
        self.assertEqual(len(result.valid), 0)
        Path(tmp).unlink()

    def _write(self, text):
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8",
        )
        f.write(text)
        f.close()
        return f.name


class TestInvalidAmount(unittest.TestCase):
    """Non-numeric and empty amounts must be caught."""

    def _load(self, amount_value):
        csv_text = (
            "transaction_id,date,description,amount,currency\n"
            f"T-001,2026-01-10,Test,{amount_value},USD\n"
        )
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8",
        )
        f.write(csv_text)
        f.close()
        result = load_csv(f.name)
        Path(f.name).unlink()
        return result

    def test_alphabetic_amount(self):
        result = self._load("abc")
        self.assertEqual(len(result.valid), 0)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("Invalid amount", result.errors[0].errors[0])

    def test_empty_amount(self):
        result = self._load("")
        self.assertEqual(len(result.valid), 0)
        self.assertIn("Missing amount", result.errors[0].errors[0])


class TestMissingColumns(unittest.TestCase):
    """A CSV missing required columns should fail at the header level."""

    def test_missing_amount_column(self):
        csv_text = "transaction_id,date,description,currency\nT-001,2026-01-10,Test,USD\n"
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8",
        )
        f.write(csv_text)
        f.close()
        result = load_csv(f.name)
        # No valid rows because we abort on missing columns.
        self.assertEqual(len(result.valid), 0)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("Missing required columns", result.errors[0].errors[0])
        Path(f.name).unlink()


class TestMultipleErrorsPerRow(unittest.TestCase):
    """If a row has two bad fields, both errors should be reported."""

    def test_bad_date_and_bad_amount(self):
        csv_text = (
            "transaction_id,date,description,amount,currency\n"
            "T-001,not-a-date,Test,not-a-number,USD\n"
        )
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8",
        )
        f.write(csv_text)
        f.close()
        result = load_csv(f.name)
        self.assertEqual(len(result.errors), 1)
        error_messages = result.errors[0].errors
        self.assertEqual(len(error_messages), 2)
        Path(f.name).unlink()


class TestFindDuplicates(unittest.TestCase):
    """Duplicate detection by transaction_id."""

    def _make_txn(self, tid, amount="100.00", row=1):
        from ledgerlens.models import Transaction
        return Transaction(
            transaction_id=tid,
            date=date(2026, 1, 1),
            description="test",
            amount=Decimal(amount),
            currency="USD",
            source_row=row,
        )

    def test_no_duplicates(self):
        txns = [self._make_txn("A"), self._make_txn("B"), self._make_txn("C")]
        unique, dupes = find_duplicates(txns)
        self.assertEqual(len(unique), 3)
        self.assertEqual(len(dupes), 0)

    def test_all_copies_of_duplicate_excluded(self):
        """If ID 'X' appears twice, BOTH copies go to duplicates."""
        txns = [
            self._make_txn("A"),
            self._make_txn("X", "100.00", row=2),
            self._make_txn("X", "200.00", row=3),
            self._make_txn("B"),
        ]
        unique, dupes = find_duplicates(txns)
        self.assertEqual(len(unique), 2)  # A and B
        self.assertEqual(len(dupes), 2)   # both X's
        self.assertTrue(all(d.transaction_id == "X" for d in dupes))

    def test_empty_list(self):
        unique, dupes = find_duplicates([])
        self.assertEqual(len(unique), 0)
        self.assertEqual(len(dupes), 0)


if __name__ == "__main__":
    unittest.main()
