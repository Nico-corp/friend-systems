#!/usr/bin/env python3
"""
Options domain evaluator.
Reads closed paper trades from options DB and computes a score.
Score = win_rate * 0.6 + avg_pnl_pct * 0.4
Prints a single float to stdout.
"""
import sys
import os
import sqlite3

WORKSPACE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

DB_CANDIDATES = [
    os.path.join(WORKSPACE, "options", "options.db"),
    os.path.join(WORKSPACE, "options", "options_trades.db"),
]

def find_db():
    for path in DB_CANDIDATES:
        if os.path.exists(path):
            return path
    return None

def score_from_db(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Try to discover the trades table
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]

    trade_table = None
    for candidate in ["trades", "paper_trades", "options_trades", "closed_trades"]:
        if candidate in tables:
            trade_table = candidate
            break
    if trade_table is None and tables:
        trade_table = tables[0]

    if trade_table is None:
        conn.close()
        return None

    cur.execute(f"PRAGMA table_info({trade_table})")
    columns = [r[1] for r in cur.fetchall()]

    # Need pnl or pnl_pct and a status/closed indicator
    pnl_col = next((c for c in columns if c in ("pnl_pct", "pnl_percent", "return_pct")), None)
    if pnl_col is None:
        pnl_col = next((c for c in columns if "pnl" in c.lower()), None)
    status_col = next((c for c in columns if c in ("status", "state", "is_closed", "closed")), None)

    if pnl_col is None:
        conn.close()
        return None

    if status_col:
        cur.execute(f"SELECT {pnl_col} FROM {trade_table} WHERE {status_col} IN ('closed','CLOSED','1',1)")
    else:
        cur.execute(f"SELECT {pnl_col} FROM {trade_table}")

    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows if r[0] is not None]

def main():
    db_path = find_db()
    if db_path is None:
        print("0.0")
        sys.exit(0)

    pnl_values = score_from_db(db_path)

    if pnl_values is None or len(pnl_values) < 10:
        print("0.0")
        sys.exit(0)

    wins = sum(1 for v in pnl_values if v > 0)
    win_rate = wins / len(pnl_values)
    avg_pnl_pct = sum(pnl_values) / len(pnl_values)

    score = win_rate * 0.6 + avg_pnl_pct * 0.4
    print(f"{score:.6f}")

if __name__ == "__main__":
    main()
