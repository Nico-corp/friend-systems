#!/usr/bin/env python3
"""
Signal Validation Engine (Agent Strength Initiative #1)

Learns from realized P&L which pre-entry metrics predict wins.
Scoreboard tracks: pre-entry metrics → post-exit outcomes → weekly correlation analysis.

Schema: signal_scoreboard.db
- signals table: id, timestamp, strategy, ticker, pre_iv, pre_momentum, pre_volume, conviction_score, entry_price
- outcomes table: signal_id, exit_price, pnl, hold_seconds, win_loss, realized_return_pct
- weekly_analysis table: week_date, metric, correlation_to_wins, win_rate_when_high, sample_size, confidence_level
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".openclaw/workspace/options/data/signal_scoreboard.db"

def init_database():
    """Initialize signal scoreboard schema."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Signals table: pre-entry metrics
    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            strategy TEXT,
            ticker TEXT,
            pre_iv REAL,
            pre_momentum REAL,
            pre_volume INTEGER,
            conviction_score INTEGER,
            entry_price REAL,
            position_size REAL,
            strategy_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Outcomes table: post-exit metrics
    c.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            signal_id INTEGER PRIMARY KEY,
            exit_price REAL,
            exit_time TEXT,
            pnl REAL,
            hold_seconds INTEGER,
            win_loss TEXT,
            realized_return_pct REAL,
            exit_reason TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )
    """)
    
    # Weekly analysis results
    c.execute("""
        CREATE TABLE IF NOT EXISTS weekly_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_date TEXT,
            metric TEXT,
            correlation_to_wins REAL,
            win_rate_when_high REAL,
            win_rate_when_low REAL,
            sample_size INTEGER,
            confidence_level TEXT,
            recommendation TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()

def log_signal(strategy, ticker, pre_iv, pre_momentum, pre_volume, conviction_score, entry_price, position_size, strategy_name):
    """Log a signal at entry time."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        INSERT INTO signals (timestamp, strategy, ticker, pre_iv, pre_momentum, pre_volume, conviction_score, entry_price, position_size, strategy_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(), strategy, ticker, pre_iv, pre_momentum, pre_volume, conviction_score, entry_price, position_size, strategy_name))
    
    signal_id = c.lastrowid
    conn.commit()
    conn.close()
    
    return signal_id

def log_outcome(signal_id, exit_price, hold_seconds, realized_pnl, exit_reason):
    """Log outcome at exit time."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Get entry price for return calculation
    c.execute("SELECT entry_price FROM signals WHERE id = ?", (signal_id,))
    result = c.fetchone()
    if not result:
        conn.close()
        return
    
    entry_price = result[0]
    realized_return_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0
    win_loss = "WIN" if realized_pnl > 0 else ("LOSS" if realized_pnl < 0 else "BREAK")
    
    c.execute("""
        INSERT INTO outcomes (signal_id, exit_price, exit_time, pnl, hold_seconds, win_loss, realized_return_pct, exit_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (signal_id, exit_price, datetime.now().isoformat(), realized_pnl, hold_seconds, win_loss, realized_return_pct, exit_reason))
    
    conn.commit()
    conn.close()

def analyze_signals_weekly():
    """
    Weekly calibration: correlate pre-entry metrics with realized outcomes.
    Returns: metric analysis with confidence-weighted recommendations.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Get all closed signals from this week
    c.execute("""
        SELECT s.id, s.pre_iv, s.pre_momentum, s.pre_volume, s.conviction_score, o.win_loss
        FROM signals s
        JOIN outcomes o ON s.id = o.signal_id
        WHERE DATE(s.created_at) >= DATE('now', '-7 days')
    """)
    
    results = c.fetchall()
    if not results:
        conn.close()
        return {"status": "insufficient_data", "sample_size": 0}
    
    # Analyze each metric
    analysis = {
        "week_date": datetime.now().strftime("%Y-%m-%d"),
        "sample_size": len(results),
        "metrics": {}
    }
    
    metrics = {
        "iv": (lambda row: row[1], 2),  # pre_iv, column index 1
        "momentum": (lambda row: row[2], 3),
        "volume": (lambda row: row[3], 4),
        "conviction": (lambda row: row[4], 5),
    }
    
    for metric_name, (extractor, col_idx) in metrics.items():
        values = [extractor(row) for row in results]
        wins = sum(1 for row in results if row[5] == "WIN")
        median_val = sorted(values)[len(values) // 2] if values else 0
        
        high_val_wins = sum(1 for row in results if extractor(row) >= median_val and row[5] == "WIN")
        low_val_wins = sum(1 for row in results if extractor(row) < median_val and row[5] == "WIN")
        
        high_count = sum(1 for row in results if extractor(row) >= median_val)
        low_count = sum(1 for row in results if extractor(row) < median_val)
        
        win_rate_high = (high_val_wins / high_count * 100) if high_count > 0 else 0
        win_rate_low = (low_val_wins / low_count * 100) if low_count > 0 else 0
        
        overall_wr = (wins / len(results) * 100)
        
        # Determine confidence level based on sample size and win rate divergence
        if len(results) < 10:
            confidence = "LOW"
        elif abs(win_rate_high - win_rate_low) < 10:
            confidence = "MED"
        else:
            confidence = "HIGH"
        
        recommendation = ""
        if win_rate_high > overall_wr + 10:
            recommendation = f"TIGHTEN gate: require {metric_name} >= {median_val:.2f}"
        elif win_rate_low > overall_wr + 10:
            recommendation = f"INVERT gate: prefer {metric_name} < {median_val:.2f}"
        else:
            recommendation = f"HOLD current gate ({metric_name} is predictive at {confidence} confidence)"
        
        analysis["metrics"][metric_name] = {
            "win_rate_when_high": round(win_rate_high, 1),
            "win_rate_when_low": round(win_rate_low, 1),
            "median_threshold": round(median_val, 2),
            "confidence": confidence,
            "recommendation": recommendation,
            "sample_size": len(results)
        }
    
    # Save to database
    for metric_name, metric_data in analysis["metrics"].items():
        c.execute("""
            INSERT INTO weekly_analysis (week_date, metric, correlation_to_wins, win_rate_when_high, win_rate_when_low, sample_size, confidence_level, recommendation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis["week_date"],
            metric_name,
            0,  # correlation not yet implemented
            metric_data["win_rate_when_high"],
            metric_data["win_rate_when_low"],
            metric_data["sample_size"],
            metric_data["confidence"],
            metric_data["recommendation"]
        ))
    
    conn.commit()
    conn.close()
    
    return analysis

if __name__ == "__main__":
    init_database()
    print("✅ Signal validation engine ready")
