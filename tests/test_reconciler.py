"""Tests for reconciliation and cash-movement summaries.

Each test builds Transaction objects directly (no CSV involved)
so the expected values are trivially hand-verifiable.
"""

import unittest
from datetime import date
from decimal import Decimal

from ledgerlens.models import Transaction
from ledgerlens.reconciler import (
    compute_cash_summary,
    reconcile,
)


def _txn(tid: str, amount: str, currency: str = "USD", row: int = 1) -> Transaction:
    """Shorthand for building test transactions."""
    return Transaction(
        transaction_id=tid,
        date=date(2026, 1, 1),
        description="test",
        amount=Decimal(amount),
        currency=currency,
        source_row=row,
    )


class TestExactMatch(unittest.TestCase):
    """Transactions with identical (ID, currency, amount) → matched."""

    def test_single_match(self):
        internal = [_txn("A", "500.00")]
        bank = [_txn("A", "500.00")]
        result = reconcile(internal, bank)

        self.assertEqual(len(result.matched), 1)
        self.assertEqual(len(result.conflicts), 0)
        self.assertEqual(len(result.unmatched_internal), 0)
        self.assertEqual(len(result.unmatched_bank), 0)

    def test_multiple_matches(self):
        internal = [_txn("A", "100.00"), _txn("B", "-50.00")]
        bank = [_txn("A", "100.00"), _txn("B", "-50.00")]
        result = reconcile(internal, bank)

        self.assertEqual(len(result.matched), 2)


class TestAmountConflict(unittest.TestCase):
    """Same ID + currency but different amount → conflict."""

    def test_conflict_detected(self):
        internal = [_txn("A", "1000.00")]
        bank = [_txn("A", "1500.00")]
        result = reconcile(internal, bank)

        self.assertEqual(len(result.matched), 0)
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].internal_amount, Decimal("1000.00"))
        self.assertEqual(result.conflicts[0].bank_amount, Decimal("1500.00"))


class TestUnmatched(unittest.TestCase):
    """Transactions present in only one file."""

    def test_internal_only(self):
        internal = [_txn("A", "100.00")]
        bank = []
        result = reconcile(internal, bank)

        self.assertEqual(len(result.unmatched_internal), 1)
        self.assertEqual(result.unmatched_internal[0].transaction_id, "A")

    def test_bank_only(self):
        internal = []
        bank = [_txn("B", "-25.00")]
        result = reconcile(internal, bank)

        self.assertEqual(len(result.unmatched_bank), 1)
        self.assertEqual(result.unmatched_bank[0].transaction_id, "B")


class TestCashSummary(unittest.TestCase):
    """Hand-calculated inflow/outflow/net totals."""

    def test_mixed_flows(self):
        """
        +1000.00  inflow
         -250.00  outflow
         +150.50  inflow
          -50.00  outflow
        ────────
        Inflows:   1150.50
        Outflows:  -300.00
        Net:        850.50
        """
        txns = [
            _txn("A", "1000.00"),
            _txn("B", "-250.00"),
            _txn("C", "150.50"),
            _txn("D", "-50.00"),
        ]
        summary = compute_cash_summary(txns)

        self.assertEqual(summary.total_inflows, Decimal("1150.50"))
        self.assertEqual(summary.total_outflows, Decimal("-300.00"))
        self.assertEqual(summary.net_movement, Decimal("850.50"))
        self.assertEqual(summary.transaction_count, 4)

    def test_all_inflows(self):
        txns = [_txn("A", "100.00"), _txn("B", "200.00")]
        summary = compute_cash_summary(txns)

        self.assertEqual(summary.total_inflows, Decimal("300.00"))
        self.assertEqual(summary.total_outflows, Decimal("0"))
        self.assertEqual(summary.net_movement, Decimal("300.00"))

    def test_all_outflows(self):
        txns = [_txn("A", "-100.00"), _txn("B", "-200.00")]
        summary = compute_cash_summary(txns)

        self.assertEqual(summary.total_inflows, Decimal("0"))
        self.assertEqual(summary.total_outflows, Decimal("-300.00"))
        self.assertEqual(summary.net_movement, Decimal("-300.00"))

    def test_empty_list(self):
        summary = compute_cash_summary([])
        self.assertEqual(summary.total_inflows, Decimal("0"))
        self.assertEqual(summary.total_outflows, Decimal("0"))
        self.assertEqual(summary.net_movement, Decimal("0"))
        self.assertEqual(summary.transaction_count, 0)


class TestFullReconciliation(unittest.TestCase):
    """End-to-end reconciliation with hand-checked arithmetic.

    Scenario (designed to hit every category):
        Internal:  A(+500), B(-200), C(+1000)
        Bank:      A(+500), B(-300), D(-50)

    Expected:
        Matched:   A (+500, agrees)
        Conflict:  B (internal -200, bank -300)
        Unmatched: C (internal only), D (bank only)

    Internal cash:   +500 + (-200) + 1000 = 1300
    Bank cash:       +500 + (-300) + (-50) =  150
    Difference:      150 - 1300             = -1150

    Explained:
        conflict_diff       = -300 - (-200) = -100
        unmatched_bank_net  = -50
        unmatched_int_net   = +1000
        explained           = -100 + (-50) - 1000 = -1150  ✓
    """

    def setUp(self):
        self.internal = [
            _txn("A", "500.00"),
            _txn("B", "-200.00"),
            _txn("C", "1000.00"),
        ]
        self.bank = [
            _txn("A", "500.00"),
            _txn("B", "-300.00"),
            _txn("D", "-50.00"),
        ]
        self.result = reconcile(self.internal, self.bank)

    def test_categories(self):
        self.assertEqual(len(self.result.matched), 1)
        self.assertEqual(len(self.result.conflicts), 1)
        self.assertEqual(len(self.result.unmatched_internal), 1)
        self.assertEqual(len(self.result.unmatched_bank), 1)

    def test_matched_id(self):
        self.assertEqual(self.result.matched[0].internal.transaction_id, "A")

    def test_conflict_amounts(self):
        c = self.result.conflicts[0]
        self.assertEqual(c.internal_amount, Decimal("-200.00"))
        self.assertEqual(c.bank_amount, Decimal("-300.00"))

    def test_internal_cash_summary(self):
        s = self.result.internal_summary
        # +500 + 1000 = 1500
        self.assertEqual(s.total_inflows, Decimal("1500.00"))
        # -200
        self.assertEqual(s.total_outflows, Decimal("-200.00"))
        # 1500 - 200 = 1300
        self.assertEqual(s.net_movement, Decimal("1300.00"))

    def test_bank_cash_summary(self):
        s = self.result.bank_summary
        # +500
        self.assertEqual(s.total_inflows, Decimal("500.00"))
        # -300 + -50 = -350
        self.assertEqual(s.total_outflows, Decimal("-350.00"))
        # 500 - 350 = 150
        self.assertEqual(s.net_movement, Decimal("150.00"))

    def test_difference_is_explainable(self):
        """The algebraic identity from the plan must hold."""
        int_net = self.result.internal_summary.net_movement
        bank_net = self.result.bank_summary.net_movement
        actual_diff = bank_net - int_net  # 150 - 1300 = -1150

        conflict_diff = sum(
            (c.bank_amount - c.internal_amount for c in self.result.conflicts),
            Decimal("0"),
        )
        unmatched_bank = sum(
            (t.amount for t in self.result.unmatched_bank), Decimal("0"),
        )
        unmatched_int = sum(
            (t.amount for t in self.result.unmatched_internal), Decimal("0"),
        )
        explained = conflict_diff + unmatched_bank - unmatched_int

        self.assertEqual(explained, actual_diff)
        self.assertEqual(actual_diff, Decimal("-1150.00"))


if __name__ == "__main__":
    unittest.main()
