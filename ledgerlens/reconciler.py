"""Transaction reconciliation and cash-movement summaries.

Reconciliation algorithm (exact matching)
-----------------------------------------
1. Index bank transactions by (transaction_id, currency).
2. Walk through each internal transaction:
   a. If the key exists in the bank index AND amounts are equal
      → matched pair.
   b. If the key exists but amounts differ → conflict pair.
   c. If the key doesn't exist → unmatched internal.
3. Any bank transaction whose key was never looked up
   → unmatched bank.

This is an O(n + m) algorithm: one pass to build the dict, one pass
to probe it.  Good enough for millions of rows.

Why (transaction_id, currency) and not just transaction_id?
  In milestone 1 we only have USD, but keying on currency now means
  multi-currency support later won't require a reconciler rewrite.

What about descriptions and dates?
  Deliberately excluded from the match key.  Banks routinely rename
  descriptions and post transactions on different dates.  We compare
  amounts because a dollar mismatch is the reconciliation signal that
  matters most.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import Transaction


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------


@dataclass
class MatchedPair:
    """Two transactions that agree on ID, currency, AND amount."""

    internal: Transaction
    bank: Transaction


@dataclass
class ConflictPair:
    """Same ID and currency, but the amounts disagree.

    We store both amounts explicitly so the reporter doesn't have
    to reach back into the Transaction objects.
    """

    internal: Transaction
    bank: Transaction
    internal_amount: Decimal
    bank_amount: Decimal


@dataclass
class CashSummary:
    """Inflow / outflow / net totals for one dataset.

    These are *cash movements*, not accounting profit.  The
    distinction matters: profit adjusts for accruals and non-cash
    items; cash movement is simply money in minus money out.
    """

    total_inflows: Decimal    # sum of positive amounts
    total_outflows: Decimal   # sum of negative amounts (will be ≤ 0)
    net_movement: Decimal     # inflows + outflows
    transaction_count: int


@dataclass
class ReconciliationResult:
    """Complete output of a reconciliation run."""

    matched: list[MatchedPair] = field(default_factory=list)
    conflicts: list[ConflictPair] = field(default_factory=list)
    unmatched_internal: list[Transaction] = field(default_factory=list)
    unmatched_bank: list[Transaction] = field(default_factory=list)
    internal_summary: CashSummary | None = None
    bank_summary: CashSummary | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconcile(
    internal: list[Transaction],
    bank: list[Transaction],
) -> ReconciliationResult:
    """Match internal transactions against bank transactions.

    Both lists should already have duplicates removed — this function
    assumes each transaction_id appears at most once per list.

    Parameters
    ----------
    internal : list[Transaction]
        Unique, validated internal-ledger transactions.
    bank : list[Transaction]
        Unique, validated bank-statement transactions.

    Returns
    -------
    ReconciliationResult
        Categorised pairs plus cash-movement summaries for each side.
    """
    # Step 1: Index bank transactions for O(1) lookup.
    bank_by_key: dict[tuple[str, str], Transaction] = {}
    for txn in bank:
        key = (txn.transaction_id, txn.currency)
        bank_by_key[key] = txn

    matched: list[MatchedPair] = []
    conflicts: list[ConflictPair] = []
    unmatched_internal: list[Transaction] = []
    consumed_bank_keys: set[tuple[str, str]] = set()

    # Step 2: Probe the index with each internal transaction.
    for txn in internal:
        key = (txn.transaction_id, txn.currency)

        if key in bank_by_key:
            bank_txn = bank_by_key[key]
            consumed_bank_keys.add(key)

            if txn.amount == bank_txn.amount:
                matched.append(MatchedPair(internal=txn, bank=bank_txn))
            else:
                conflicts.append(
                    ConflictPair(
                        internal=txn,
                        bank=bank_txn,
                        internal_amount=txn.amount,
                        bank_amount=bank_txn.amount,
                    )
                )
        else:
            unmatched_internal.append(txn)

    # Step 3: Whatever the bank had that internal never asked for.
    unmatched_bank = [
        txn
        for txn in bank
        if (txn.transaction_id, txn.currency) not in consumed_bank_keys
    ]

    return ReconciliationResult(
        matched=matched,
        conflicts=conflicts,
        unmatched_internal=unmatched_internal,
        unmatched_bank=unmatched_bank,
        internal_summary=compute_cash_summary(internal),
        bank_summary=compute_cash_summary(bank),
    )


def compute_cash_summary(transactions: list[Transaction]) -> CashSummary:
    """Compute inflow, outflow, and net cash movement.

    Uses Decimal("0") as the start value for sum() so the result
    stays Decimal even when the list is empty.  (sum() with no start
    value would return int 0, which is technically fine for arithmetic
    but loses the Decimal type signal.)
    """
    inflows = sum(
        (t.amount for t in transactions if t.amount > 0),
        Decimal("0"),
    )
    outflows = sum(
        (t.amount for t in transactions if t.amount < 0),
        Decimal("0"),
    )

    return CashSummary(
        total_inflows=inflows,
        total_outflows=outflows,
        net_movement=inflows + outflows,
        transaction_count=len(transactions),
    )
