#!/usr/bin/env python3
"""
Conviction Score Calibration (Agent Strength Initiative #4)

Weekly: Measure false positives (skipped wins) and false negatives (entered losses).
Auto-tighten/loosen gates based on accuracy.

Runs weekly (Fridays 4 PM ET) via cron.

v2: MetaClaw data versioning (arXiv 2603.17187)
  - support/query split prevents train/eval contamination
  - Calibration grades accuracy on query set ONLY
  - Gate updates trained from support set ONLY
  - Assignment: if strategy signal count < 10 → support; else 80% support / 20% query
"""

import random
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path.home() / ".openclaw/workspace/options/data/conviction_calibration.db"

# MetaClaw versioning constants
DATA_VERSION        = "v2"          # Increment when schema changes
SUPPORT_THRESHOLD   = 10            # Signals per strategy before query allocation starts
QUERY_FRACTION      = 0.20          # 20% of signals go to query set once threshold reached


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table (used for safe migration)."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    return column in columns


def _safe_add_column(conn: sqlite3.Connection, table: str, column: str,
                     col_type: str, default: str = None):
    """
    Add a column to a table only if it doesn't already exist.
    SQLite does not support ALTER TABLE ... ADD COLUMN IF NOT EXISTS natively,
    so we check via PRAGMA.
    """
    if not _column_exists(conn, table, column):
        default_clause = f" DEFAULT {default}" if default is not None else ""
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}{default_clause}")


def init_database():
    """Initialize conviction calibration schema (with safe migration for v2 columns)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Skipped signals: scored but didn't enter (false positives = would have won)
    c.execute("""
        CREATE TABLE IF NOT EXISTS skipped_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            strategy TEXT,
            ticker TEXT,
            conviction_score INTEGER,
            reason_skipped TEXT,
            retrospective_pnl REAL,
            was_winning TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Entered signals: scored and traded (false negatives = actually lost)
    c.execute("""
        CREATE TABLE IF NOT EXISTS entered_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            strategy TEXT,
            ticker TEXT,
            conviction_score INTEGER,
            realized_pnl REAL,
            win_loss TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Weekly calibration results
    c.execute("""
        CREATE TABLE IF NOT EXISTS weekly_calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_date TEXT,
            total_signals_evaluated INTEGER,
            total_entered INTEGER,
            win_rate REAL,
            false_positives_count INTEGER,
            false_positives_pnl REAL,
            false_negatives_count INTEGER,
            false_negatives_pnl REAL,
            gate_recommendation TEXT,
            new_threshold INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()

    # -------------------------------------------------------------------------
    # v2 safe migration: add MetaClaw versioning columns
    # -------------------------------------------------------------------------
    _safe_add_column(conn, "skipped_signals", "data_version",   "TEXT", f"'{DATA_VERSION}'")
    _safe_add_column(conn, "skipped_signals", "evaluation_set", "TEXT", "'support'")
    _safe_add_column(conn, "entered_signals", "data_version",   "TEXT", f"'{DATA_VERSION}'")
    _safe_add_column(conn, "entered_signals", "evaluation_set", "TEXT", "'support'")

    # Weekly calibration v2 columns
    _safe_add_column(conn, "weekly_calibration", "support_count", "INTEGER", "0")
    _safe_add_column(conn, "weekly_calibration", "query_count",   "INTEGER", "0")

    conn.commit()
    conn.close()

def _assign_evaluation_set(conn: sqlite3.Connection, strategy: str) -> str:
    """
    Assign evaluation_set for a new signal.
    Rule: if total signal count for strategy < SUPPORT_THRESHOLD → 'support'
    Otherwise: 80% 'support' / 20% 'query' (random assignment).
    """
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) FROM (
            SELECT id FROM skipped_signals WHERE strategy=?
            UNION ALL
            SELECT id FROM entered_signals WHERE strategy=?
        )
    """, (strategy, strategy))
    count = c.fetchone()[0]

    if count < SUPPORT_THRESHOLD:
        return "support"
    else:
        return "query" if random.random() < QUERY_FRACTION else "support"


def log_skipped_signal(strategy, ticker, conviction_score, reason):
    """Log a signal that we skipped (gate didn't pass or intentional skip)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    eval_set = _assign_evaluation_set(conn, strategy)

    c.execute("""
        INSERT INTO skipped_signals
            (timestamp, strategy, ticker, conviction_score, reason_skipped, was_winning,
             data_version, evaluation_set)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(), strategy, ticker, conviction_score, reason,
          "UNKNOWN", DATA_VERSION, eval_set))
    
    conn.commit()
    conn.close()

def log_entered_signal(strategy, ticker, conviction_score, realized_pnl):
    """Log a signal that we entered."""
    win_loss = "WIN" if realized_pnl > 0 else ("LOSS" if realized_pnl < 0 else "BREAK")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    eval_set = _assign_evaluation_set(conn, strategy)
    
    c.execute("""
        INSERT INTO entered_signals
            (timestamp, strategy, ticker, conviction_score, realized_pnl, win_loss,
             data_version, evaluation_set)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(), strategy, ticker, conviction_score, realized_pnl,
          win_loss, DATA_VERSION, eval_set))
    
    conn.commit()
    conn.close()

def mark_skipped_as_winning(skipped_id, retrospective_pnl):
    """Retroactively mark a skipped signal as winning (regret)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        UPDATE skipped_signals
        SET was_winning='YES', retrospective_pnl=?
        WHERE id=?
    """, (retrospective_pnl, skipped_id))
    
    conn.commit()
    conn.close()

def run_weekly_calibration():
    """
    MetaClaw v2: Support/query separated calibration.

    - ACCURACY graded on query set ONLY (prevents eval contamination)
    - GATE updates trained from support set ONLY
    - Both counts logged in weekly_calibration table

    Also runs the legacy weekly_calibration_report() logic for backward compatibility.
    Returns the calibration report dict.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    week_start = (datetime.now() - timedelta(days=7)).isoformat()

    # -------------------------------------------------------------------------
    # Count support vs query this week
    # -------------------------------------------------------------------------
    c.execute("""
        SELECT COUNT(*) FROM (
            SELECT evaluation_set FROM skipped_signals WHERE timestamp >= ?
            UNION ALL
            SELECT evaluation_set FROM entered_signals WHERE timestamp >= ?
        ) WHERE evaluation_set='support'
    """, (week_start, week_start))
    support_count = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(*) FROM (
            SELECT evaluation_set FROM skipped_signals WHERE timestamp >= ?
            UNION ALL
            SELECT evaluation_set FROM entered_signals WHERE timestamp >= ?
        ) WHERE evaluation_set='query'
    """, (week_start, week_start))
    query_count = c.fetchone()[0]

    # -------------------------------------------------------------------------
    # ACCURACY: grade on QUERY set only
    # -------------------------------------------------------------------------
    c.execute("""
        SELECT COUNT(*), COALESCE(SUM(retrospective_pnl), 0)
        FROM skipped_signals
        WHERE was_winning='YES' AND timestamp >= ? AND evaluation_set='query'
    """, (week_start,))
    regret_count, regret_pnl = c.fetchone()

    c.execute("""
        SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0)
        FROM entered_signals
        WHERE win_loss='LOSS' AND timestamp >= ? AND evaluation_set='query'
    """, (week_start,))
    misfire_count, misfire_pnl = c.fetchone()

    c.execute("""
        SELECT COUNT(*) FROM entered_signals
        WHERE timestamp >= ? AND evaluation_set='query'
    """, (week_start,))
    total_entered_query = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(*) FROM skipped_signals
        WHERE timestamp >= ? AND evaluation_set='query'
    """, (week_start,))
    total_skipped_query = c.fetchone()[0]

    c.execute("""
        SELECT SUM(CASE WHEN win_loss='WIN' THEN 1 ELSE 0 END)
        FROM entered_signals
        WHERE timestamp >= ? AND evaluation_set='query'
    """, (week_start,))
    total_wins_query = c.fetchone()[0] or 0

    win_rate   = (total_wins_query / total_entered_query * 100) if total_entered_query > 0 else 0
    regret_pct = (regret_count / max(total_skipped_query, 1) * 100)
    misfire_pct = (misfire_count / max(total_entered_query, 1) * 100)

    # -------------------------------------------------------------------------
    # GATE UPDATES: train from SUPPORT set only
    # -------------------------------------------------------------------------
    c.execute("""
        SELECT COUNT(*), SUM(CASE WHEN win_loss='WIN' THEN 1 ELSE 0 END)
        FROM entered_signals
        WHERE timestamp >= ? AND evaluation_set='support'
    """, (week_start,))
    support_entered_row = c.fetchone()
    support_entered  = support_entered_row[0] or 0
    support_wins     = support_entered_row[1] or 0

    c.execute("""
        SELECT COUNT(*) FROM skipped_signals
        WHERE was_winning='YES' AND timestamp >= ? AND evaluation_set='support'
    """, (week_start,))
    support_regret = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(*) FROM entered_signals
        WHERE win_loss='LOSS' AND timestamp >= ? AND evaluation_set='support'
    """, (week_start,))
    support_misfires = c.fetchone()[0]

    support_regret_pct  = (support_regret  / max(support_count - support_entered, 1) * 100)
    support_misfire_pct = (support_misfires / max(support_entered, 1) * 100)

    # Gate recommendation (derived from SUPPORT set)
    if support_regret_pct > 30:
        recommendation = "LOOSEN gate (too many skipped winners in support set)"
        new_threshold = max(4, min(10,
            (support_wins + support_regret) //
            max(1, (support_wins + support_regret + support_misfires))
        ))
    elif support_misfire_pct > 40:
        recommendation = "TIGHTEN gate (too many misfires in support set)"
        new_threshold = max(4, min(10, support_wins // max(1, support_entered)))
    else:
        recommendation = "HOLD current gate (balanced support-set performance)"
        new_threshold = 7

    # -------------------------------------------------------------------------
    # Save to weekly_calibration
    # -------------------------------------------------------------------------
    total_query_signals = total_skipped_query + total_entered_query

    c.execute("""
        INSERT INTO weekly_calibration (
            week_date, total_signals_evaluated, total_entered,
            win_rate, false_positives_count, false_positives_pnl,
            false_negatives_count, false_negatives_pnl,
            gate_recommendation, new_threshold,
            support_count, query_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d"),
        support_count + query_count,
        total_entered_query + support_entered,
        round(win_rate, 1),
        regret_count,
        round(regret_pnl, 2),
        misfire_count,
        round(misfire_pnl, 2),
        recommendation,
        new_threshold,
        support_count,
        query_count,
    ))

    conn.commit()
    conn.close()

    return {
        "week": datetime.now().strftime("%Y-%m-%d"),
        "metaclaw_versioning": {
            "support_count": support_count,
            "query_count":   query_count,
            "data_version":  DATA_VERSION,
        },
        "accuracy_on_query_set": {
            "total_query_signals": total_query_signals,
            "entered": total_entered_query,
            "win_rate": round(win_rate, 1),
            "regret": {
                "count": regret_count,
                "pnl": round(regret_pnl, 2),
                "pct_of_skipped": round(regret_pct, 1),
            },
            "misfire": {
                "count": misfire_count,
                "pnl": round(misfire_pnl, 2),
                "pct_of_entered": round(misfire_pct, 1),
            },
        },
        "gate_trained_on_support": {
            "recommendation": recommendation,
            "new_conviction_threshold": new_threshold,
        },
    }


# Backward-compatible alias
def weekly_calibration_report():
    """Alias for run_weekly_calibration() — kept for backward compat."""
    return run_weekly_calibration()


def get_contamination_report() -> dict:
    """
    MetaClaw contamination check: verify no signal appears in both
    support AND query sets (should be impossible by design, but verify).

    Signals are deduplicated by (strategy, ticker, timestamp) across both tables.
    Returns a report with contamination status and any violating records.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Check skipped_signals for rows that somehow have both sets
    # (can't happen with INSERT-time assignment, but check for data corruption)
    c.execute("""
        SELECT id, strategy, ticker, timestamp, evaluation_set
        FROM skipped_signals
        ORDER BY timestamp DESC
    """)
    skipped_rows = {(r[1], r[2], r[3][:16]): r[4] for r in c.fetchall()}

    c.execute("""
        SELECT id, strategy, ticker, timestamp, evaluation_set
        FROM entered_signals
        ORDER BY timestamp DESC
    """)
    entered_rows = {(r[1], r[2], r[3][:16]): r[4] for r in c.fetchall()}

    # Look for same (strategy, ticker, timestamp-minute) across BOTH tables
    # (very unlikely but possible if a signal was logged twice)
    skipped_keys = set(skipped_rows.keys())
    entered_keys = set(entered_rows.keys())
    overlap      = skipped_keys & entered_keys

    contaminated = []
    for key in overlap:
        s_set = skipped_rows[key]
        e_set = entered_rows[key]
        if s_set != e_set:
            contaminated.append({
                "key":          key,
                "skipped_set":  s_set,
                "entered_set":  e_set,
                "note":         "Same signal in both tables with different eval sets"
            })

    conn.close()

    return {
        "status":            "CLEAN" if not contaminated else "CONTAMINATED",
        "total_skipped":     len(skipped_rows),
        "total_entered":     len(entered_rows),
        "overlap_count":     len(overlap),
        "contaminated_count": len(contaminated),
        "violations":        contaminated,
        "checked_at":        datetime.now().isoformat(),
    }


if __name__ == "__main__":
    init_database()
    print("✅ Conviction calibrator v2 ready (MetaClaw support/query versioning active)")
    print(f"   DATA_VERSION={DATA_VERSION} | support_threshold={SUPPORT_THRESHOLD} | query_fraction={QUERY_FRACTION}")
    report = get_contamination_report()
    print(f"   Contamination check: {report['status']} "
          f"({report['total_skipped']} skipped, {report['total_entered']} entered)")
