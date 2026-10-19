"""LedgerLens — Interactive Streamlit Dashboard for Transaction Reconciliation.

Features:
1. Ingest sample fictional datasets or upload custom CSV files.
2. Preview validation errors and quarantined duplicate records.
3. Select explicit internal and bank batches with metadata (filename, timestamp, batch ID).
4. Run reconciliation on-demand via a single button click.
5. Display financial KPI cards:
   - Internal and bank net cash movements
   - Net difference
   - "Difference fully explained" (algebraic proof) separated from "Net Cash Movements Equal"
   - Match, conflict, and unmatched transaction counts
6. Searchable result tables (exact matches, conflicts, unmatched) with source row and filename tracing.
7. Monthly inflow/outflow charts for selected batches, with excluded duplicates clearly labeled.
8. One-click downloads for reconciliation CSVs, summary report, and SQL report.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from ledgerlens.database import (
    cents_to_decimal,
    get_batches,
    get_connection,
    import_csv_to_db,
    init_db,
    reconcile_batches_in_db,
)
from ledgerlens.loader import find_duplicates, load_csv
from ledgerlens.reconciler import reconcile
from ledgerlens.reporter import build_summary_text, export_all
from ledgerlens.sql_reports import (
    format_sql_reports_text,
    get_amount_conflicts,
    get_data_quality_summary,
    get_monthly_cash_movements,
    get_reconciliation_overview,
    get_unmatched_summary,
)

# ---------------------------------------------------------------------------
# Database Helpers
# ---------------------------------------------------------------------------

DB_PATH = Path("ledgerlens.db")


def get_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = get_connection(db_path)
    init_db(conn)
    return conn


def fetch_matched_records(db_conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    cursor = db_conn.cursor()
    cursor.execute(
        """
        SELECT
            r.transaction_id,
            r.currency,
            r.internal_amount_cents,
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
        WHERE r.run_id = ? AND r.category = 'matched'
        ORDER BY r.transaction_id ASC;
        """,
        (run_id,),
    )
    results = []
    for row in cursor.fetchall():
        d = dict(row)
        d["amount"] = str(cents_to_decimal(d["internal_amount_cents"]))
        results.append(d)
    return results


def fetch_unmatched_records(
    db_conn: sqlite3.Connection, run_id: int, category: str
) -> list[dict[str, Any]]:
    cursor = db_conn.cursor()
    if category == "unmatched_internal":
        cursor.execute(
            """
            SELECT
                r.transaction_id,
                r.currency,
                r.internal_amount_cents AS amount_cents,
                it.date,
                it.description,
                it.source_row,
                it.source_filename
            FROM reconciliation_results r
            LEFT JOIN validated_transactions it ON r.internal_txn_id = it.id
            WHERE r.run_id = ? AND r.category = 'unmatched_internal'
            ORDER BY r.transaction_id ASC;
            """,
            (run_id,),
        )
    else:
        cursor.execute(
            """
            SELECT
                r.transaction_id,
                r.currency,
                r.bank_amount_cents AS amount_cents,
                bt.date,
                bt.description,
                bt.source_row,
                bt.source_filename
            FROM reconciliation_results r
            LEFT JOIN validated_transactions bt ON r.bank_txn_id = bt.id
            WHERE r.run_id = ? AND r.category = 'unmatched_bank'
            ORDER BY r.transaction_id ASC;
            """,
            (run_id,),
        )
    results = []
    for row in cursor.fetchall():
        d = dict(row)
        d["amount"] = str(cents_to_decimal(d["amount_cents"]))
        results.append(d)
    return results


def fetch_batch_records(
    db_conn: sqlite3.Connection, table: str, batch_id: int
) -> list[dict[str, Any]]:
    cursor = db_conn.cursor()
    cursor.execute(f"SELECT * FROM {table} WHERE batch_id = ? ORDER BY source_row ASC;", (batch_id,))
    rows = [dict(r) for r in cursor.fetchall()]
    for r in rows:
        if "amount_cents" in r:
            r["amount"] = str(cents_to_decimal(r["amount_cents"]))
    return rows


# ---------------------------------------------------------------------------
# UI Entry Point
# ---------------------------------------------------------------------------


def run_dashboard():
    # Page Configuration & Styling
    st.set_page_config(
        page_title="LedgerLens — Transaction Reconciliation",
        page_icon="🔍",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown(
        """
        <style>
        .metric-card {
            background-color: #f8f9fa;
            border: 1px solid #e9ecef;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 12px;
        }
        .status-badge-success {
            background-color: #d4edda;
            color: #155724;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 4px;
            display: inline-block;
        }
        .status-badge-warning {
            background-color: #fff3cd;
            color: #856404;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 4px;
            display: inline-block;
        }
        .status-badge-danger {
            background-color: #f8d7da;
            color: #721c24;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 4px;
            display: inline-block;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    conn = get_db()

    # Session State Initialization
    if "active_run_id" not in st.session_state:
        st.session_state["active_run_id"] = None
    if "selected_int_batch" not in st.session_state:
        st.session_state["selected_int_batch"] = None
    if "selected_bank_batch" not in st.session_state:
        st.session_state["selected_bank_batch"] = None
    if "notification_msg" not in st.session_state:
        st.session_state["notification_msg"] = None

    # Sidebar: Batch Ingestion & Selection
    with st.sidebar:
        st.title("⚙️ Data & Batches")

        st.subheader("1. Ingest Transactions")
        ingest_mode = st.radio("Source:", ["Fictional Sample Data", "Upload CSV Files"])

        if ingest_mode == "Fictional Sample Data":
            st.info("Uses standard sample files:\n- internal_transactions.csv\n- bank_transactions.csv")
            if st.button("📥 Import Sample Data", use_container_width=True):
                try:
                    int_csv = Path("data/internal_transactions.csv")
                    bank_csv = Path("data/bank_transactions.csv")
                    b_int, is_new_int = import_csv_to_db(conn, int_csv, "internal")
                    b_bank, is_new_bank = import_csv_to_db(conn, bank_csv, "bank")

                    st.session_state["selected_int_batch"] = b_int
                    st.session_state["selected_bank_batch"] = b_bank

                    msg = (
                        f"Internal Batch #{b_int} ({'New' if is_new_int else 'Already imported'}), "
                        f"Bank Batch #{b_bank} ({'New' if is_new_bank else 'Already imported'})."
                    )
                    st.session_state["notification_msg"] = ("success", msg)
                    st.rerun()
                except Exception as ex:
                    st.session_state["notification_msg"] = ("error", f"Import failed: {ex}")

        else:
            st.caption("Upload separate Internal and Bank CSVs:")
            up_internal = st.file_uploader("Internal Ledger CSV", type=["csv"], key="uploader_int")
            up_bank = st.file_uploader("Bank Statement CSV", type=["csv"], key="uploader_bank")

            if st.button("📥 Import Uploaded Files", use_container_width=True):
                if not up_internal or not up_bank:
                    st.error("Please upload both Internal and Bank CSV files.")
                else:
                    try:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f_int:
                            f_int.write(up_internal.getvalue())
                            f_int_path = Path(f_int.name)

                        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f_bank:
                            f_bank.write(up_bank.getvalue())
                            f_bank_path = Path(f_bank.name)

                        b_int, is_new_int = import_csv_to_db(conn, f_int_path, "internal")
                        b_bank, is_new_bank = import_csv_to_db(conn, f_bank_path, "bank")

                        f_int_path.unlink(missing_ok=True)
                        f_bank_path.unlink(missing_ok=True)

                        st.session_state["selected_int_batch"] = b_int
                        st.session_state["selected_bank_batch"] = b_bank

                        msg = (
                            f"Uploaded files imported: Internal Batch #{b_int} ({'New' if is_new_int else 'Duplicate skipped'}), "
                            f"Bank Batch #{b_bank} ({'New' if is_new_bank else 'Duplicate skipped'})."
                        )
                        st.session_state["notification_msg"] = ("success", msg)
                        st.rerun()
                    except Exception as ex:
                        st.session_state["notification_msg"] = ("error", f"Import failed: {ex}")

        st.markdown("---")
        st.subheader("2. Select Batches to Reconcile")

        int_batches = get_batches(conn, "internal")
        bank_batches = get_batches(conn, "bank")

        if not int_batches or not bank_batches:
            st.warning("No batches available. Import data above to get started.")
            selected_int_batch = None
            selected_bank_batch = None
        else:
            int_options = {
                b["id"]: f"Batch #{b['id']}: {b['source_filename']} ({b['imported_at'][:19]})"
                for b in int_batches
            }
            bank_options = {
                b["id"]: f"Batch #{b['id']}: {b['source_filename']} ({b['imported_at'][:19]})"
                for b in bank_batches
            }

            default_int = (
                st.session_state["selected_int_batch"]
                if st.session_state["selected_int_batch"] in int_options
                else list(int_options.keys())[-1]
            )
            default_bank = (
                st.session_state["selected_bank_batch"]
                if st.session_state["selected_bank_batch"] in bank_options
                else list(bank_options.keys())[-1]
            )

            selected_int_batch = st.selectbox(
                "Internal Batch:",
                options=list(int_options.keys()),
                format_func=lambda x: int_options[x],
                index=list(int_options.keys()).index(default_int),
            )
            selected_bank_batch = st.selectbox(
                "Bank Batch:",
                options=list(bank_options.keys()),
                format_func=lambda x: bank_options[x],
                index=list(bank_options.keys()).index(default_bank),
            )

            st.session_state["selected_int_batch"] = selected_int_batch
            st.session_state["selected_bank_batch"] = selected_bank_batch

        st.markdown("---")
        st.subheader("3. Action")
        run_btn = st.button("🚀 Run Reconciliation", type="primary", use_container_width=True)

        if run_btn:
            if not selected_int_batch or not selected_bank_batch:
                st.error("Please select both an internal and bank batch first.")
            else:
                try:
                    run_id = reconcile_batches_in_db(conn, selected_int_batch, selected_bank_batch)
                    st.session_state["active_run_id"] = run_id
                    st.session_state["notification_msg"] = (
                        "success",
                        f"Reconciliation completed successfully! Run #{run_id} created.",
                    )
                    st.rerun()
                except Exception as ex:
                    st.session_state["notification_msg"] = ("error", f"Reconciliation failed: {ex}")

    # Main Content
    st.title("LedgerLens 🔍 Financial Reconciliation Dashboard")
    st.caption(
        "Automated reconciliation, data-quality auditing, and algebraic cash movement verification."
    )

    if st.session_state["notification_msg"]:
        level, text = st.session_state["notification_msg"]
        if level == "success":
            st.success(text)
        elif level == "error":
            st.error(text)
        st.session_state["notification_msg"] = None

    if not selected_int_batch or not selected_bank_batch:
        st.info(
            "👋 Welcome! To begin, click **'Import Sample Data'** in the sidebar to load the "
            "fictional datasets and run reconciliation."
        )
        return

    # Section A: Pre-Reconciliation Data Quality & Previews
    with st.expander("📋 Pre-Reconciliation Data Quality & Batch Previews", expanded=False):
        st.write(
            f"Inspecting **Internal Batch #{selected_int_batch}** and **Bank Batch #{selected_bank_batch}**:"
        )

        quality = get_data_quality_summary(conn, [selected_int_batch, selected_bank_batch])
        col_q1, col_q2 = st.columns(2)

        for idx, q in enumerate(quality):
            target_col = col_q1 if idx == 0 else col_q2
            with target_col:
                st.markdown(f"**Batch #{q['batch_id']} ({q['dataset_side'].upper()} - `{q['source_filename']}`)**")
                m1, m2, m3 = st.columns(3)
                m1.metric("Accepted Rows", q["accepted_count"])
                m2.metric("Duplicates (Quarantined)", q["duplicate_count"])
                m3.metric("Validation Errors", q["invalid_count"])

        tab_err, tab_dup, tab_valid = st.tabs([
            "⚠️ Validation Errors",
            "👥 Quarantined Duplicates",
            "✅ Accepted Transactions",
        ])

        with tab_err:
            int_errs = fetch_batch_records(conn, "validation_errors", selected_int_batch)
            bank_errs = fetch_batch_records(conn, "validation_errors", selected_bank_batch)
            all_errs = int_errs + bank_errs
            if not all_errs:
                st.success("No validation errors in selected batches.")
            else:
                df_err = pd.DataFrame(all_errs)[[
                    "source_filename", "dataset_side", "source_row", "errors", "raw_data_json"
                ]]
                st.dataframe(df_err, use_container_width=True)

        with tab_dup:
            int_dups = fetch_batch_records(conn, "duplicate_records", selected_int_batch)
            bank_dups = fetch_batch_records(conn, "duplicate_records", selected_bank_batch)
            all_dups = int_dups + bank_dups
            if not all_dups:
                st.success("No duplicate transaction IDs in selected batches.")
            else:
                df_dup = pd.DataFrame(all_dups)[[
                    "source_filename", "dataset_side", "source_row", "transaction_id", "date", "amount", "description"
                ]]
                st.dataframe(df_dup, use_container_width=True)

        with tab_valid:
            int_val = fetch_batch_records(conn, "validated_transactions", selected_int_batch)
            bank_val = fetch_batch_records(conn, "validated_transactions", selected_bank_batch)
            df_valid = pd.DataFrame(int_val + bank_val)[[
                "source_filename", "dataset_side", "source_row", "transaction_id", "date", "amount", "currency", "description"
            ]]
            st.dataframe(df_valid, use_container_width=True)

    # Section B: Reconciliation Results & Financial Proof
    active_run_id = st.session_state["active_run_id"]

    if active_run_id is None:
        st.warning("👈 Click **'Run Reconciliation'** in the sidebar to perform matching and generate reports.")
        return

    overview = get_reconciliation_overview(conn, active_run_id)

    st.markdown("### 📊 Financial Summary & Reconciliation Status")

    # Key Metrics Row 1: Balances vs Explanation
    c_stat1, c_stat2, c_stat3 = st.columns([1.5, 1.5, 2])

    with c_stat1:
        is_balances_equal = (overview["net_difference"] == Decimal("0.00"))
        badge_class = "status-badge-success" if is_balances_equal else "status-badge-warning"
        badge_label = "YES ✓" if is_balances_equal else f"NO (Variance: ${overview['net_difference']:,.2f})"
        st.markdown(
            f"""
            <div class="metric-card">
                <div style="font-size: 13px; color: #6c757d; font-weight: 600;">NET CASH MOVEMENTS EQUAL?</div>
                <div style="margin-top: 8px;"><span class="{badge_class}">{badge_label}</span></div>
                <div style="font-size: 12px; color: #6c757d; margin-top: 6px;">Compares raw internal net to bank net.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c_stat2:
        is_fully_explained = overview["is_difference_fully_explained"]
        badge_class = "status-badge-success" if is_fully_explained else "status-badge-danger"
        badge_label = "YES ✓" if is_fully_explained else "NO ✗ (Unexplained Variance!)"
        st.markdown(
            f"""
            <div class="metric-card">
                <div style="font-size: 13px; color: #6c757d; font-weight: 600;">DIFFERENCE FULLY EXPLAINED?</div>
                <div style="margin-top: 8px;"><span class="{badge_class}">{badge_label}</span></div>
                <div style="font-size: 12px; color: #6c757d; margin-top: 6px;">Algebraic proof that variance is 100% accounted for.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c_stat3:
        st.markdown(
            f"""
            <div class="metric-card">
                <div style="font-size: 13px; color: #6c757d; font-weight: 600;">NET CASH MOVEMENTS</div>
                <div style="display: flex; justify-content: space-between; margin-top: 6px;">
                    <span>Internal Net: <b>${overview['internal_net']:,.2f}</b></span>
                    <span>Bank Net: <b>${overview['bank_net']:,.2f}</b></span>
                </div>
                <div style="font-size: 12px; color: #495057; margin-top: 4px;">
                    Net Variance (Bank − Internal): <b>${overview['net_difference']:+,.2f}</b>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Key Metrics Row 2: Category Counts
    m_cnt1, m_cnt2, m_cnt3, m_cnt4 = st.columns(4)
    m_cnt1.metric("Exact Matches", overview["matched_count"])
    m_cnt2.metric("Amount Conflicts", overview["conflict_count"])
    m_cnt3.metric("Unmatched (Internal)", overview["unmatched_internal_count"])
    m_cnt4.metric("Unmatched (Bank)", overview["unmatched_bank_count"])

    # Algebraic Explanation Box
    with st.expander("📐 View Algebraic Reconciliation Proof Details", expanded=False):
        st.markdown(
            f"""
            **Variance Equation:**
            $$\\text{{Bank Net}} - \\text{{Internal Net}} = \\text{{Net Difference}}$$
            $$\\${overview['bank_net']:,.2f} - \\${overview['internal_net']:,.2f} = \\mathbf{{\\${overview['net_difference']:,.2f}}}$$

            **Explanation Breakdown:**
            - **Amount Conflicts Difference** (Bank − Internal): `+${overview['conflict_diff']:,.2f}`
            - **Unmatched Bank Net**: `+${overview['unmatched_bank_net']:,.2f}`
            - **Unmatched Internal Net**: `-${overview['unmatched_internal_net']:,.2f}`
            - **Total Explained**: `+${overview['explained_difference']:,.2f}`
            - **Difference Fully Explained**: **{'YES ✓' if overview['is_difference_fully_explained'] else 'NO ✗'}**
            """
        )

    # Section C: Searchable Result Tables
    st.markdown("---")
    st.markdown("### 🔎 Searchable Reconciliation Tables")

    search_query = st.text_input(
        "Search transactions across all categories:",
        placeholder="Type transaction ID (e.g. TXN-003) or description...",
    ).strip().lower()

    tab_matched, tab_conflict, tab_unmatched_int, tab_unmatched_bank = st.tabs([
        f"✅ Exact Matches ({overview['matched_count']})",
        f"⚠️ Amount Conflicts ({overview['conflict_count']})",
        f"⬅️ Unmatched Internal ({overview['unmatched_internal_count']})",
        f"➡️ Unmatched Bank ({overview['unmatched_bank_count']})",
    ])

    matches = fetch_matched_records(conn, active_run_id)
    conflicts = get_amount_conflicts(conn, active_run_id)
    unmatched_int = fetch_unmatched_records(conn, active_run_id, "unmatched_internal")
    unmatched_bank = fetch_unmatched_records(conn, active_run_id, "unmatched_bank")

    with tab_matched:
        filtered_matches = matches
        if search_query:
            filtered_matches = [
                m for m in matches
                if search_query in m["transaction_id"].lower()
                or search_query in m["internal_description"].lower()
                or search_query in m["bank_description"].lower()
            ]
        if not filtered_matches:
            st.write("No matching records found.")
        else:
            df_m = pd.DataFrame(filtered_matches)[[
                "transaction_id", "amount", "currency",
                "internal_date", "internal_description", "internal_row", "internal_filename",
                "bank_date", "bank_description", "bank_row", "bank_filename",
            ]]
            st.dataframe(df_m, use_container_width=True)

    with tab_conflict:
        filtered_conflicts = conflicts
        if search_query:
            filtered_conflicts = [
                c for c in conflicts
                if search_query in c["transaction_id"].lower()
                or search_query in c["internal_description"].lower()
                or search_query in c["bank_description"].lower()
            ]
        if not filtered_conflicts:
            st.write("No conflicting records found.")
        else:
            df_c = pd.DataFrame(filtered_conflicts)[[
                "transaction_id", "currency", "internal_amount", "bank_amount", "difference",
                "internal_date", "internal_description", "internal_row", "internal_filename",
                "bank_date", "bank_description", "bank_row", "bank_filename",
            ]]
            st.dataframe(df_c, use_container_width=True)

    with tab_unmatched_int:
        filtered_ui = unmatched_int
        if search_query:
            filtered_ui = [
                u for u in unmatched_int
                if search_query in u["transaction_id"].lower()
                or search_query in u["description"].lower()
            ]
        if not filtered_ui:
            st.write("No unmatched internal records found.")
        else:
            df_ui = pd.DataFrame(filtered_ui)[[
                "transaction_id", "date", "description", "amount", "currency",
                "source_row", "source_filename",
            ]]
            st.dataframe(df_ui, use_container_width=True)

    with tab_unmatched_bank:
        filtered_ub = unmatched_bank
        if search_query:
            filtered_ub = [
                u for u in unmatched_bank
                if search_query in u["transaction_id"].lower()
                or search_query in u["description"].lower()
            ]
        if not filtered_ub:
            st.write("No unmatched bank records found.")
        else:
            df_ub = pd.DataFrame(filtered_ub)[[
                "transaction_id", "date", "description", "amount", "currency",
                "source_row", "source_filename",
            ]]
            st.dataframe(df_ub, use_container_width=True)

    # Section D: Monthly Cash Movement Charts
    st.markdown("---")
    st.markdown("### 📈 Monthly Cash Movement Visualizations")
    st.caption(
        "Visualizes inflows and outflows for the selected internal and bank batches only. "
        "Calculations are strictly maintained in Decimal/integer cents; values are converted to float solely for charting."
    )

    monthly_data = get_monthly_cash_movements(conn, [selected_int_batch, selected_bank_batch])
    accepted_movements = [m for m in monthly_data if m["record_status"] == "accepted"]
    excluded_duplicates = [m for m in monthly_data if m["record_status"] == "excluded_duplicate"]

    if accepted_movements:
        chart_records = []
        for m in accepted_movements:
            chart_records.append({
                "Series": f"{m['dataset_side'].capitalize()} Inflows",
                "Month": m["month"],
                "Amount": float(m["inflows"]),
            })
            chart_records.append({
                "Series": f"{m['dataset_side'].capitalize()} Outflows",
                "Month": m["month"],
                "Amount": float(abs(m["outflows"])),
            })
        for d in excluded_duplicates:
            chart_records.append({
                "Series": f"{d['dataset_side'].capitalize()} Excluded Duplicate Records",
                "Month": d["month"],
                "Amount": float(abs(d["net_movement"])),
            })

        df_chart = pd.DataFrame(chart_records)
        st.bar_chart(
            df_chart,
            x="Month",
            y="Amount",
            color="Series",
            use_container_width=True,
        )

        if excluded_duplicates:
            st.warning(
                "⚠️ **Excluded Duplicate Records**: "
                + "; ".join(
                    f"{d['dataset_side'].capitalize()} duplicate records total ${d['net_movement']:,.2f} ({d['txn_count']} rows)"
                    for d in excluded_duplicates
                )
                + ". These are data-quality issues excluded from reconciliation, NOT confirmed cash movements."
            )

    # Section E: Report Downloads
    st.markdown("---")
    st.markdown("### 📥 Download Reconciliation Artifacts")

    col_d1, col_d2, col_d3, col_d4 = st.columns(4)
    sql_report_content = format_sql_reports_text(conn, active_run_id)

    with col_d1:
        st.download_button(
            "📄 Download SQL Report (.txt)",
            data=sql_report_content,
            file_name=f"sql_report_run_{active_run_id}.txt",
            mime="text/plain",
            use_container_width=True,
        )

    with col_d2:
        if matches:
            csv_m = pd.DataFrame(matches).to_csv(index=False).encode("utf-8")
            st.download_button(
                "📊 Download Matches (.csv)",
                data=csv_m,
                file_name=f"matched_run_{active_run_id}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    with col_d3:
        if conflicts:
            csv_c = pd.DataFrame(conflicts).to_csv(index=False).encode("utf-8")
            st.download_button(
                "⚠️ Download Conflicts (.csv)",
                data=csv_c,
                file_name=f"conflicts_run_{active_run_id}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    with col_d4:
        unmatched_combined = unmatched_int + unmatched_bank
        if unmatched_combined:
            csv_u = pd.DataFrame(unmatched_combined).to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬅️➡️ Download Unmatched (.csv)",
                data=csv_u,
                file_name=f"unmatched_run_{active_run_id}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    st.caption("LedgerLens Milestone 3 — Local Streamlit Dashboard. Standard Library + Streamlit.")


if __name__ == "__main__":
    run_dashboard()
