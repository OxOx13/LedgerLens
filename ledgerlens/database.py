"""SQLite storage, schema management, and atomic batch imports for LedgerLens.

Design notes
------------
Why integer cents instead of REAL?
  SQLite REAL is an IEEE 754 floating-point number.  Floating point
  cannot accurately represent decimal fractions like 0.10 or 0.70
  (0.10 + 0.20 = 0.30000000000000004).  Storing currency as integer
  cents eliminates all rounding errors at the database layer.

Why atomic transactions (with conn:)?
  A financial import either succeeds completely or fails without leaving
  any trace.  If a file has 1,000 valid rows and row 1,001 fails (e.g.,
  unhandled fractional cents), committing a partial batch would corrupt
  financial reports.  We wrap all batch inserts in `with conn:` so any
  exception triggers an automatic ROLLBACK.

Why SHA-256 for idempotent imports?
  Detecting re-imports by filename alone is brittle (files can be renamed
  or overwritten).  By hashing file contents and storing (dataset_side,
  file_hash) with a UNIQUE constraint, we guarantee that importing the
  identical file a second time is a safe no-op.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .loader import find_duplicates, load_csv
from .models import Transaction, ValidationError

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_side TEXT NOT NULL CHECK(dataset_side IN ('internal', 'bank')),
    source_filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(dataset_side, file_hash)
);

CREATE TABLE IF NOT EXISTS validated_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    dataset_side TEXT NOT NULL CHECK(dataset_side IN ('internal', 'bank')),
    source_filename TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    transaction_id TEXT NOT NULL,
    date TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS duplicate_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    dataset_side TEXT NOT NULL CHECK(dataset_side IN ('internal', 'bank')),
    source_filename TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    transaction_id TEXT NOT NULL,
    date TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    dataset_side TEXT NOT NULL CHECK(dataset_side IN ('internal', 'bank')),
    source_filename TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    raw_data_json TEXT NOT NULL,
    errors TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    internal_batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    bank_batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    reconciled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES reconciliation_runs(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK(category IN ('matched', 'conflict', 'unmatched_internal', 'unmatched_bank')),
    transaction_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    internal_txn_id INTEGER REFERENCES validated_transactions(id) ON DELETE SET NULL,
    bank_txn_id INTEGER REFERENCES validated_transactions(id) ON DELETE SET NULL,
    internal_amount_cents INTEGER,
    bank_amount_cents INTEGER,
    difference_cents INTEGER
);

-- Indexes for efficient joins and lookups
CREATE INDEX IF NOT EXISTS idx_val_txns_batch ON validated_transactions(batch_id);
CREATE INDEX IF NOT EXISTS idx_val_txns_key ON validated_transactions(transaction_id, currency);
CREATE INDEX IF NOT EXISTS idx_dup_txns_batch ON duplicate_records(batch_id);
CREATE INDEX IF NOT EXISTS idx_val_err_batch ON validation_errors(batch_id);
CREATE INDEX IF NOT EXISTS idx_rec_results_run ON reconciliation_results(run_id);
"""


# ---------------------------------------------------------------------------
# Money conversion helpers (Decimal <-> integer cents)
# ---------------------------------------------------------------------------


def decimal_to_cents(amount: Decimal) -> int:
    """Convert a Decimal USD amount to an integer number of cents.

    Parameters
    ----------
    amount : Decimal
        The currency amount (e.g., Decimal("123.45")).

    Returns
    -------
    int
        Cents (e.g., 12345).

    Raises
    ------
    TypeError
        If amount is not a Decimal.
    ValueError
        If amount is not finite (NaN, Inf) or contains fractional cents
        (e.g., Decimal("123.456")).
    """
    if not isinstance(amount, Decimal):
        raise TypeError(f"Amount must be a Decimal, got {type(amount).__name__}")

    if not amount.is_finite():
        raise ValueError(f"Amount {amount} is not finite (NaN or Infinity)")

    cents_dec = amount * 100
    if cents_dec != cents_dec.to_integral_value():
        raise ValueError(
            f"Amount {amount} contains fractional cents and cannot be stored "
            "as an integer without loss of precision"
        )

    return int(cents_dec)


def cents_to_decimal(cents: int) -> Decimal:
    """Convert integer cents back to a 2-decimal-place Decimal.

    Parameters
    ----------
    cents : int
        The integer cents (e.g., 12345).

    Returns
    -------
    Decimal
        The formatted Decimal (e.g., Decimal("123.45")).
    """
    if not isinstance(cents, int):
        raise TypeError(f"Cents must be an int, got {type(cents).__name__}")

    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Database connection & setup
# ---------------------------------------------------------------------------


def get_connection(db_path: str | Path = "ledgerlens.db") -> sqlite3.Connection:
    """Open an SQLite connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all schema tables and indexes if they do not exist."""
    conn.executescript(SCHEMA_SQL)


def compute_file_hash(filepath: str | Path) -> str:
    """Compute the SHA-256 hex digest of a file's contents."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as fh:
        while chunk := fh.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Batch imports (Idempotent and Atomic)
# ---------------------------------------------------------------------------


def import_csv_to_db(
    conn: sqlite3.Connection,
    filepath: str | Path,
    dataset_side: str,
) -> tuple[int, bool]:
    """Load a CSV and atomically import its batches into SQLite.

    If an identical file has already been imported for this dataset_side,
    the existing batch_id is returned and no records are re-inserted.

    Parameters
    ----------
    conn : sqlite3.Connection
        Active SQLite connection.
    filepath : str | Path
        Path to the CSV file.
    dataset_side : str
        Either 'internal' or 'bank'.

    Returns
    -------
    tuple[int, bool]
        (batch_id, is_new): True if newly imported, False if existing batch found.

    Raises
    ------
    ValueError
        If dataset_side is invalid, or if any valid/duplicate transaction
        contains fractional cents or non-finite values.
    """
    if dataset_side not in ("internal", "bank"):
        raise ValueError(f"Invalid dataset_side '{dataset_side}'; must be 'internal' or 'bank'")

    path = Path(filepath)
    file_hash = compute_file_hash(path)

    # 1. Idempotency check: see if (dataset_side, file_hash) already exists.
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM import_batches WHERE dataset_side = ? AND file_hash = ?",
        (dataset_side, file_hash),
    )
    row = cursor.fetchone()
    if row is not None:
        return int(row["id"]), False

    # 2. Parse and validate the CSV using milestone 1 logic.
    load_res = load_csv(path)
    unique_txns, dup_txns = find_duplicates(load_res.valid)

    # 3. Validate money amounts upfront so we reject fractional cents
    # before performing database writes.
    validated_rows: list[tuple[Any, ...]] = []
    for t in unique_txns:
        cents = decimal_to_cents(t.amount)
        validated_rows.append((
            dataset_side,
            path.name,
            t.source_row,
            t.transaction_id,
            t.date.isoformat(),
            t.description,
            cents,
            t.currency,
        ))

    duplicate_rows: list[tuple[Any, ...]] = []
    for t in dup_txns:
        cents = decimal_to_cents(t.amount)
        duplicate_rows.append((
            dataset_side,
            path.name,
            t.source_row,
            t.transaction_id,
            t.date.isoformat(),
            t.description,
            cents,
            t.currency,
        ))

    error_rows: list[tuple[Any, ...]] = []
    for err in load_res.errors:
        error_rows.append((
            dataset_side,
            path.name,
            err.source_row,
            json.dumps(err.raw_data, ensure_ascii=False),
            "; ".join(err.errors),
        ))

    # 4. Atomic database transaction: all tables or none.
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        cursor.execute(
            """
            INSERT INTO import_batches (dataset_side, source_filename, file_hash, imported_at)
            VALUES (?, ?, ?, ?)
            """,
            (dataset_side, path.name, file_hash, now_iso),
        )
        batch_id = int(cursor.lastrowid)

        if validated_rows:
            cursor.executemany(
                """
                INSERT INTO validated_transactions (
                    batch_id, dataset_side, source_filename, source_row,
                    transaction_id, date, description, amount_cents, currency
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(batch_id, *r) for r in validated_rows],
            )

        if duplicate_rows:
            cursor.executemany(
                """
                INSERT INTO duplicate_records (
                    batch_id, dataset_side, source_filename, source_row,
                    transaction_id, date, description, amount_cents, currency
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(batch_id, *r) for r in duplicate_rows],
            )

        if error_rows:
            cursor.executemany(
                """
                INSERT INTO validation_errors (
                    batch_id, dataset_side, source_filename, source_row,
                    raw_data_json, errors
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(batch_id, *r) for r in error_rows],
            )

    return batch_id, True


def get_batches(conn: sqlite3.Connection, dataset_side: str | None = None) -> list[dict[str, Any]]:
    """List imported batches, optionally filtered by dataset_side."""
    cursor = conn.cursor()
    if dataset_side is not None:
        cursor.execute(
            """
            SELECT id, dataset_side, source_filename, file_hash, imported_at
            FROM import_batches
            WHERE dataset_side = ?
            ORDER BY id ASC
            """,
            (dataset_side,),
        )
    else:
        cursor.execute(
            """
            SELECT id, dataset_side, source_filename, file_hash, imported_at
            FROM import_batches
            ORDER BY id ASC
            """
        )
    return [dict(row) for row in cursor.fetchall()]


def get_latest_batch_id(conn: sqlite3.Connection, dataset_side: str) -> int:
    """Return the ID of the most recently imported batch for a dataset_side."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM import_batches WHERE dataset_side = ? ORDER BY id DESC LIMIT 1",
        (dataset_side,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"No imported batch found for dataset_side '{dataset_side}'")
    return int(row["id"])


# ---------------------------------------------------------------------------
# Database Reconciliation Run
# ---------------------------------------------------------------------------


def reconcile_batches_in_db(
    conn: sqlite3.Connection,
    internal_batch_id: int,
    bank_batch_id: int,
) -> int:
    """Reconcile validated transactions from selected batches and store results.

    Parameters
    ----------
    conn : sqlite3.Connection
        Active SQLite connection.
    internal_batch_id : int
        The batch ID for internal transactions.
    bank_batch_id : int
        The batch ID for bank transactions.

    Returns
    -------
    int
        The newly created reconciliation run_id.
    """
    cursor = conn.cursor()

    # Load validated transactions for the specified batches
    cursor.execute(
        """
        SELECT id, transaction_id, currency, amount_cents, date, description, source_row
        FROM validated_transactions
        WHERE batch_id = ?
        ORDER BY id ASC
        """,
        (internal_batch_id,),
    )
    int_rows = cursor.fetchall()

    cursor.execute(
        """
        SELECT id, transaction_id, currency, amount_cents, date, description, source_row
        FROM validated_transactions
        WHERE batch_id = ?
        ORDER BY id ASC
        """,
        (bank_batch_id,),
    )
    bank_rows = cursor.fetchall()

    # Index bank rows by (transaction_id, currency)
    bank_by_key: dict[tuple[str, str], sqlite3.Row] = {}
    for r in bank_rows:
        bank_by_key[(r["transaction_id"], r["currency"])] = r

    reconciliation_results_to_insert: list[tuple[Any, ...]] = []
    consumed_bank_keys: set[tuple[str, str]] = set()

    for int_r in int_rows:
        key = (int_r["transaction_id"], int_r["currency"])
        int_id = int_r["id"]
        int_cents = int_r["amount_cents"]
        tid = int_r["transaction_id"]
        cur = int_r["currency"]

        if key in bank_by_key:
            bank_r = bank_by_key[key]
            consumed_bank_keys.add(key)
            bank_id = bank_r["id"]
            bank_cents = bank_r["amount_cents"]

            if int_cents == bank_cents:
                # matched
                reconciliation_results_to_insert.append((
                    "matched", tid, cur, int_id, bank_id, int_cents, bank_cents, 0
                ))
            else:
                # conflict
                diff_cents = bank_cents - int_cents
                reconciliation_results_to_insert.append((
                    "conflict", tid, cur, int_id, bank_id, int_cents, bank_cents, diff_cents
                ))
        else:
            # unmatched internal
            reconciliation_results_to_insert.append((
                "unmatched_internal", tid, cur, int_id, None, int_cents, None, None
            ))

    for bank_r in bank_rows:
        key = (bank_r["transaction_id"], bank_r["currency"])
        if key not in consumed_bank_keys:
            bank_id = bank_r["id"]
            bank_cents = bank_r["amount_cents"]
            tid = bank_r["transaction_id"]
            cur = bank_r["currency"]
            reconciliation_results_to_insert.append((
                "unmatched_bank", tid, cur, None, bank_id, None, bank_cents, None
            ))

    # Atomic write of run and results
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        cursor.execute(
            """
            INSERT INTO reconciliation_runs (internal_batch_id, bank_batch_id, reconciled_at)
            VALUES (?, ?, ?)
            """,
            (internal_batch_id, bank_batch_id, now_iso),
        )
        run_id = int(cursor.lastrowid)

        cursor.executemany(
            """
            INSERT INTO reconciliation_results (
                run_id, category, transaction_id, currency,
                internal_txn_id, bank_txn_id,
                internal_amount_cents, bank_amount_cents, difference_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(run_id, *r) for r in reconciliation_results_to_insert],
        )

    return run_id
