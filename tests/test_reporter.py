"""Tests for the reporter module.

These verify that exported files are created and contain the
expected content.  We use a temporary directory so tests never
pollute the project's output/ folder.
"""

import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from ledgerlens.models import Transaction, ValidationError
from ledgerlens.reconciler import reconcile
from ledgerlens.reporter import export_all


def _txn(tid: str, amount: str, row: int = 1) -> Transaction:
    return Transaction(
        transaction_id=tid,
        date=date(2026, 1, 1),
        description="test",
        amount=Decimal(amount),
        currency="USD",
        source_row=row,
    )


class TestExportCreatesAllFiles(unittest.TestCase):
    """Running export_all should produce exactly 9 output files."""

    def test_all_files_exist(self):
        internal = [_txn("A", "100.00"), _txn("B", "-50.00")]
        bank = [_txn("A", "100.00"), _txn("C", "-25.00")]
        result = reconcile(internal, bank)

        with tempfile.TemporaryDirectory() as tmpdir:
            export_all(
                result=result,
                internal_duplicates=[],
                bank_duplicates=[],
                internal_errors=[],
                bank_errors=[],
                output_dir=Path(tmpdir),
            )
            files = sorted(f.name for f in Path(tmpdir).iterdir())

        expected = sorted([
            "conflicts.csv",
            "duplicates_bank.csv",
            "duplicates_internal.csv",
            "invalid_bank.csv",
            "invalid_internal.csv",
            "matched.csv",
            "summary.txt",
            "unmatched_bank.csv",
            "unmatched_internal.csv",
        ])
        self.assertEqual(files, expected)


class TestMatchedCSVContent(unittest.TestCase):
    """Matched.csv should have one data row per matched pair."""

    def test_matched_row_content(self):
        internal = [_txn("A", "100.00")]
        bank = [_txn("A", "100.00")]
        result = reconcile(internal, bank)

        with tempfile.TemporaryDirectory() as tmpdir:
            export_all(
                result=result,
                internal_duplicates=[],
                bank_duplicates=[],
                internal_errors=[],
                bank_errors=[],
                output_dir=Path(tmpdir),
            )
            with open(Path(tmpdir) / "matched.csv", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transaction_id"], "A")
        self.assertEqual(rows[0]["amount"], "100.00")


class TestConflictsCSVContent(unittest.TestCase):
    """Conflicts.csv must show both amounts and the difference."""

    def test_conflict_difference(self):
        internal = [_txn("A", "1000.00")]
        bank = [_txn("A", "1200.00")]
        result = reconcile(internal, bank)

        with tempfile.TemporaryDirectory() as tmpdir:
            export_all(
                result=result,
                internal_duplicates=[],
                bank_duplicates=[],
                internal_errors=[],
                bank_errors=[],
                output_dir=Path(tmpdir),
            )
            with open(Path(tmpdir) / "conflicts.csv", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount_internal"], "1000.00")
        self.assertEqual(rows[0]["amount_bank"], "1200.00")
        self.assertEqual(rows[0]["difference"], "200.00")


class TestInvalidCSVContent(unittest.TestCase):
    """Invalid rows should preserve raw data and error messages."""

    def test_error_preserved(self):
        err = ValidationError(
            source_row=5,
            raw_data={
                "transaction_id": "BAD",
                "date": "not-a-date",
                "description": "oops",
                "amount": "abc",
                "currency": "USD",
            },
            errors=["Invalid date: 'not-a-date'", "Invalid amount: 'abc'"],
        )
        # Need a minimal reconciliation result
        result = reconcile([], [])

        with tempfile.TemporaryDirectory() as tmpdir:
            export_all(
                result=result,
                internal_duplicates=[],
                bank_duplicates=[],
                internal_errors=[err],
                bank_errors=[],
                output_dir=Path(tmpdir),
            )
            with open(Path(tmpdir) / "invalid_internal.csv", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_row"], "5")
        self.assertIn("Invalid date", rows[0]["errors"])
        self.assertIn("Invalid amount", rows[0]["errors"])


class TestSummaryContent(unittest.TestCase):
    """The summary.txt should contain key reconciliation facts."""

    def test_summary_mentions_difference_explained(self):
        internal = [_txn("A", "100.00")]
        bank = [_txn("A", "100.00")]
        result = reconcile(internal, bank)

        with tempfile.TemporaryDirectory() as tmpdir:
            export_all(
                result=result,
                internal_duplicates=[],
                bank_duplicates=[],
                internal_errors=[],
                bank_errors=[],
                output_dir=Path(tmpdir),
            )
            summary = (Path(tmpdir) / "summary.txt").read_text(encoding="utf-8")

        self.assertIn("Difference fully explained: YES", summary)
        self.assertIn("Exact matches:", summary)

    def test_summary_reports_duplicates(self):
        internal = [_txn("A", "100.00")]
        bank = [_txn("A", "100.00")]
        dupes = [_txn("X", "50.00", row=2), _txn("X", "50.00", row=3)]
        result = reconcile(internal, bank)

        with tempfile.TemporaryDirectory() as tmpdir:
            export_all(
                result=result,
                internal_duplicates=dupes,
                bank_duplicates=[],
                internal_errors=[],
                bank_errors=[],
                output_dir=Path(tmpdir),
            )
            summary = (Path(tmpdir) / "summary.txt").read_text(encoding="utf-8")

        self.assertIn("Duplicate IDs", summary)
        self.assertIn("2 rows", summary)


if __name__ == "__main__":
    unittest.main()
