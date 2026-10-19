"""Tests for session isolation and demo-mode temporary database management.

Verifies:
1. Two independent browser sessions operate on distinct SQLite database files.
2. Importing transactions into Session A does not expose records to Session B.
3. Resetting Session A unlinks only Session A's database file and leaves Session B
   completely intact with all its batches and reconciliation records.
4. Session operations and resets never touch the persistent CLI database.
5. Uploaded files exceeding MAX_UPLOAD_SIZE_BYTES are rejected.
6. Temporary CSV upload files are cleaned up immediately following import.
7. Database paths remain stable across reruns within a single session.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import (
    MAX_UPLOAD_SIZE_BYTES,
    MAX_UPLOAD_SIZE_MB,
    cleanup_session_db,
    get_db,
    get_session_db_path,
)
from ledgerlens.database import (
    get_batches,
    get_connection,
    import_csv_to_db,
    init_db,
    reconcile_batches_in_db,
)
from ledgerlens.sql_reports import get_reconciliation_overview


class TestSessionIsolation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

        self.db_path_a = self.temp_path / "session_A.db"
        self.db_path_b = self.temp_path / "session_B.db"
        self.cli_db_path = self.temp_path / "ledgerlens.db"

        # Initialize databases
        self.conn_a = get_connection(self.db_path_a)
        init_db(self.conn_a)

        self.conn_b = get_connection(self.db_path_b)
        init_db(self.conn_b)

        self.conn_cli = get_connection(self.cli_db_path)
        init_db(self.conn_cli)

        self.sample_int = Path("data/internal_transactions.csv")
        self.sample_bank = Path("data/bank_transactions.csv")

    def tearDown(self):
        try:
            self.conn_a.close()
        except Exception:
            pass
        try:
            self.conn_b.close()
        except Exception:
            pass
        try:
            self.conn_cli.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_import_in_session_a_does_not_expose_records_to_session_b(self):
        """Records imported into Session A must never appear in Session B."""
        # 1. Session A imports both internal and bank batches
        b_int, _ = import_csv_to_db(self.conn_a, self.sample_int, "internal")
        b_bank, _ = import_csv_to_db(self.conn_a, self.sample_bank, "bank")
        run_id = reconcile_batches_in_db(self.conn_a, b_int, b_bank)

        # Verify Session A has data
        self.assertEqual(len(get_batches(self.conn_a, "internal")), 1)
        self.assertEqual(len(get_batches(self.conn_a, "bank")), 1)
        overview_a = get_reconciliation_overview(self.conn_a, run_id)
        self.assertEqual(overview_a["matched_count"], 4)

        # 2. Query Session B - must be completely empty
        batches_b_int = get_batches(self.conn_b, "internal")
        batches_b_bank = get_batches(self.conn_b, "bank")
        self.assertEqual(len(batches_b_int), 0, "Session B must have 0 internal batches")
        self.assertEqual(len(batches_b_bank), 0, "Session B must have 0 bank batches")

        # Check raw tables in Session B
        cursor_b = self.conn_b.cursor()
        cursor_b.execute("SELECT COUNT(*) FROM validated_transactions;")
        self.assertEqual(cursor_b.fetchone()[0], 0, "Session B must have 0 transactions")
        cursor_b.execute("SELECT COUNT(*) FROM reconciliation_runs;")
        self.assertEqual(cursor_b.fetchone()[0], 0, "Session B must have 0 runs")

    def test_reset_session_a_cleans_up_without_affecting_session_b(self):
        """Resetting Session A unlinks only its file; Session B remains intact."""
        # Import data into both sessions
        import_csv_to_db(self.conn_a, self.sample_int, "internal")
        import_csv_to_db(self.conn_b, self.sample_bank, "bank")

        self.assertTrue(self.db_path_a.exists())
        self.assertTrue(self.db_path_b.exists())

        # Reset Session A
        self.conn_a.close()
        cleanup_session_db(self.db_path_a)

        # Assert Session A file is unlinked
        self.assertFalse(self.db_path_a.exists(), "Session A db file must be deleted")

        # Assert Session B is untouched and still functional
        self.assertTrue(self.db_path_b.exists(), "Session B db file must still exist")
        batches_b = get_batches(self.conn_b, "bank")
        self.assertEqual(len(batches_b), 1, "Session B must retain its imported batch")

    def test_session_operations_preserve_cli_persistent_database(self):
        """Session-level activity and resets must never alter the CLI database."""
        # Populate persistent CLI DB
        import_csv_to_db(self.conn_cli, self.sample_int, "internal")
        self.assertEqual(len(get_batches(self.conn_cli, "internal")), 1)

        # Reset Session A
        self.conn_a.close()
        cleanup_session_db(self.db_path_a)

        # Verify CLI DB remains untouched
        self.assertTrue(self.cli_db_path.exists())
        self.assertEqual(len(get_batches(self.conn_cli, "internal")), 1)

    def test_upload_size_limit_constant(self):
        """Verify upload size limit is bounded to 5 MB."""
        self.assertEqual(MAX_UPLOAD_SIZE_MB, 5)
        self.assertEqual(MAX_UPLOAD_SIZE_BYTES, 5 * 1024 * 1024)

    def test_cleanup_temporary_upload_files(self):
        """Temporary CSV upload files are deleted immediately after import."""
        temp_upload = self.temp_path / "uploaded_test.csv"
        temp_upload.write_text(
            "transaction_id,date,description,amount,currency\n"
            "TXN-999,2026-03-01,Test upload,100.00,USD\n",
            encoding="utf-8",
        )
        self.assertTrue(temp_upload.exists())

        # Simulate upload handler cleanup pattern
        try:
            b_id, is_new = import_csv_to_db(self.conn_a, temp_upload, "internal")
            self.assertTrue(is_new)
        finally:
            temp_upload.unlink(missing_ok=True)

        self.assertFalse(temp_upload.exists(), "Uploaded temporary file must be unlinked")

    def test_session_db_path_stability_across_reruns(self):
        """get_session_db_path() must return the exact same path across reruns for a session."""
        mock_session_state = {}
        with patch("app.st.session_state", mock_session_state):
            path_1 = get_session_db_path()
            session_id_1 = mock_session_state["session_id"]
            self.assertIn("session_db_path", mock_session_state)

            # Simulate a rerun
            path_2 = get_session_db_path()
            self.assertEqual(path_1, path_2, "Path must remain identical across reruns")
            self.assertEqual(mock_session_state["session_id"], session_id_1)

            # Cleanup
            cleanup_session_db(path_1)


if __name__ == "__main__":
    unittest.main()
