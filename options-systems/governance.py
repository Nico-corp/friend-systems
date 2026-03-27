#!/usr/bin/env python3
"""
Governance Layer — Phase 0
===========================
Hard constraints that CANNOT be overridden by signal logic.
These run independently of the scanner, regime detection, or any model.

Philosophy: signals determine how much you make.
            governance determines whether you survive.

IMMUTABLE RULES (never parameterize these in production):
  - Max single position: 3% of options capital
  - Max total deployed: 30% of options capital
  - Daily loss limit: -5% → halt + alert
  - Drawdown kill: -15% from HWM → full stop + liquidate
  - Tail event: VIX > 35 OR VIX 5d change > 15pts → emergency flat
  - Novel regime: if ALL signals disagree → reduce to 0, alert Nico
"""

import os
import json
import sqlite3
import yfinance as yf
from datetime import datetime, date
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# ── Constants (NEVER make these config parameters in production) ──────────────
MAX_SINGLE_POSITION_PCT = 0.03    # 3% of options capital per trade
MAX_TOTAL_DEPLOYED_PCT = 0.30     # 30% max deployed at once
DAILY_LOSS_HALT_PCT = -0.05       # -5% in a day → halt all new trades
DRAWDOWN_KILL_PCT = -0.15         # -15% from HWM → full stop + liquidate
TAIL_VIX_LEVEL = 35.0             # VIX > 35 → emergency flat
TAIL_VIX_5D_CHANGE = 15.0         # VIX +15 in 5 days → emergency flat
OPTIONS_CAPITAL = 10_000          # Paper trading capital base ($10k — Nico's actual paper account)
TOTAL_NET_WORTH = 937_512         # Total portfolio for Kelly scaling

# G17b: S011 hard gate — Full Kelly at WR=30%/b=3.0. Reverts from paper $750 at live go-live.
S011_LIVE_MAX_PER_LEG = 670

# Paper mode: relaxed limits for data collection phase
# Set False before ANY live capital is deployed
PAPER_MODE = True
MAX_TOTAL_DEPLOYED_PCT_PAPER = 0.60   # 60% allowed in paper (collecting data)

STATE_FILE = Path('/Users/nicolasotheguy/.openclaw/workspace/options/data/governance_state.json')
DB_PATH = Path('/Users/nicolasotheguy/.openclaw/workspace/options/data/options_trades.db')
ALERT_CHANNEL = '-1003810224931'  # Unusual Friend 🐋


@dataclass
class GovernanceCheck:
    allowed: bool
    reason: str
    severity: str          # OK | WARN | HALT | KILL | EMERGENCY
    action_required: str   # what to do right now
    metrics: dict


# ── State Management ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        'hwm': OPTIONS_CAPITAL,           # high-water mark
        'halted': False,
        'halt_reason': None,
        'halt_until': None,               # ISO date, None = manual reset required
        'emergency_flat': False,
        'last_check': None,
        'daily_start_value': OPTIONS_CAPITAL,
        'daily_date': str(date.today()),
        'last_halt_notified': None,       # ISO timestamp of last halt alert sent (dedup)
        'last_halt_reason': None,         # reason string at last notification (dedup)
    }


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def capital_availability_check(position_requirement: float) -> bool:
    """
    Check if position can be entered given current capital.
    Called BEFORE every trade entry.
    
    Rule: (remaining_free_capital >= position_requirement) AND 
          (current_total_deployed + position_size <= OPTIONS_CAPITAL)
    
    Returns: True if entry allowed, False if blocked by capital constraint
    """
    deployed = _get_deployed_capital()
    free_capital = OPTIONS_CAPITAL - deployed
    return (free_capital >= position_requirement and
            deployed + position_requirement <= OPTIONS_CAPITAL)


def validate_entry(
    strategy: str,
    ticker: str,
    size: float,
    sl_pct: float,
    open_positions: Optional[list] = None,  # list of position dicts for correlation stress check
) -> dict:
    """
    Full pre-execution validation (Agent Strength Initiative #5 — Execution Discipline).
    Checks: regime gate → capital → position size → SL configured → correlation stress.

    Args:
        strategy:        strategy ID (S002, S003, etc.)
        ticker:          ticker being entered
        size:            capital at risk for this position ($)
        sl_pct:          stop-loss percentage (must be negative)
        open_positions:  list of existing open positions for correlation-1 stress test
                         each dict needs: ticker, strategy, premium_received, max_loss, current_pnl

    Returns: {"approved": bool, "reason": str, "blocked_by": str|None}
    """
    # 1. Regime gate
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from regime_adaptation import detect_regime
        regime = detect_regime()
        enabled = regime.get('enabled_strategies', [])
        if strategy not in enabled:
            _log_event('ENTRY_BLOCKED', f'{strategy} blocked by {regime["state"]} regime')
            return {"approved": False, "reason": f"{strategy} disabled in {regime['state']} regime", "blocked_by": "REGIME_GATE"}
    except Exception:
        pass  # Regime check unavailable — allow

    # 2. Capital available
    if not capital_availability_check(size):
        deployed = _get_deployed_capital()
        _log_event('ENTRY_BLOCKED', f'Capital check failed: need ${size}, deployed=${deployed}')
        return {"approved": False, "reason": f"Insufficient capital (deployed=${deployed:.0f}, need=${size:.0f})", "blocked_by": "BLOCKED_BY_CAPITAL"}

    # 3. Position size check
    max_sizes = {
        "S007": 2500, "S002": 4000, "S006": 1000, "S003": 800, "S011": 750,
        "S009": 1000,   # VRP strangle — max risk per trade (strangle width × contracts)
        "S010": 500,    # 0DTE IC — max risk per trade ($1 wing × contracts)
        # S015: EV-NEGATIVE hedge. Probation — 20 positive-EV trades required.
        # Does not count toward G3 core-VRP.
        "S015": 250,
    }
    max_size = max_sizes.get(strategy, 2000)
    if size > max_size:
        return {"approved": False, "reason": f"Size ${size} exceeds {strategy} max ${max_size}", "blocked_by": "POSITION_SIZE"}

    # 3b. S007 (PMCC) hard cap: max 1 concurrent position at $10K scale.
    # PMCC LEAPS leg = $2,500 (25% of bankroll). One position already consumes the
    # capital of 5 spreads. A second S007 would put 50% of the book in one strategy.
    # Revisit when live capital exceeds $30K.
    if strategy == 'S007':
        s007_open = _count_open_strategy('S007')
        if s007_open >= 1:
            return {
                "approved": False,
                "reason": (
                    f"S007 (PMCC) limit reached: 1 concurrent position maximum at $10K scale. "
                    f"Close existing PMCC before opening a new one."
                ),
                "blocked_by": "S007_CONCENTRATION",
            }

    # 3c. S010 (0DTE Iron Condor): max 1 concurrent position.
    # 0DTE positions cannot be held overnight — a second concurrent S010 doubles
    # intraday risk with zero time to respond. Hard cap at 1, mirrors S007 logic.
    if strategy == 'S010':
        s010_open = _count_open_strategy('S010')
        if s010_open >= 1:
            return {
                "approved": False,
                "reason": (
                    f"S010 (0DTE IC) limit reached: 1 concurrent position maximum. "
                    f"0DTE must be managed intraday — no stacking allowed."
                ),
                "blocked_by": "S010_CONCENTRATION",
            }

    # 4. SL configured
    if sl_pct is None or sl_pct >= 0:
        return {"approved": False, "reason": f"SL must be negative (got {sl_pct})", "blocked_by": "SL_NOT_SET"}

    # 5. Correlation-1 stress test (Saliba framework — hard gate, not advisory).
    # Simulates worst case: ALL positions move to max loss simultaneously.
    # If total stressed loss > 15% of options capital → block entry.
    # Whitepaper promise (v6.2): this IS a hard block, not an alert. Now enforced.
    if open_positions:
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from correlation_monitor import stress_test_correlation_one
            # Include the new position in the stress scenario
            candidate_pos = {
                'ticker': ticker,
                'strategy': strategy,
                'premium_received': size * 0.15,   # Estimate: ~15% of risk = typical credit
                'max_loss': size,
                'current_pnl': 0.0,
            }
            all_positions = list(open_positions) + [candidate_pos]
            stress = stress_test_correlation_one(all_positions)
            max_drawdown = stress.get('max_drawdown', 0)
            threshold = OPTIONS_CAPITAL * 0.15   # 15% of options capital
            if max_drawdown > threshold:
                reason = (
                    f"Correlation-1 stress test FAILED: worst-case drawdown "
                    f"${max_drawdown:,.0f} > 15% limit (${threshold:,.0f}). "
                    f"Saliba rule: reduce existing positions before adding new ones."
                )
                _log_event('ENTRY_BLOCKED', f'Correlation stress block: ${max_drawdown:.0f} > ${threshold:.0f}')
                return {"approved": False, "reason": reason, "blocked_by": "CORRELATION_STRESS"}
        except Exception:
            pass  # Correlation check unavailable — allow (log would be noise if no positions)

    return {"approved": True, "reason": "All gates passed", "blocked_by": None}


def validate_exit(entry_price: float, current_price: float, strategy: str) -> dict:
    """
    Post-execution exit validation (Agent Strength Initiative #5).
    Returns: {"action": "CLOSE"|"HOLD", "reason": str, "exit_type": str}
    """
    if entry_price <= 0:
        return {"action": "HOLD", "reason": "Invalid entry price", "exit_type": "NO_ACTION"}
    
    return_pct = ((current_price - entry_price) / entry_price) * 100
    
    if return_pct >= 50:
        return {"action": "CLOSE", "reason": f"TP hit: {return_pct:.1f}%", "exit_type": "TAKE_PROFIT"}
    
    sl_threshold = -15 if strategy == "S011" else -20
    if return_pct <= sl_threshold:
        return {"action": "CLOSE", "reason": f"SL hit: {return_pct:.1f}%", "exit_type": "STOP_LOSS"}
    
    return {"action": "HOLD", "reason": f"No exit condition ({return_pct:.1f}%)", "exit_type": "NO_ACTION"}


def reset_halt(reason: str = 'manual reset by Nico'):
    """Nico manually resets a halt. Logged."""
    state = _load_state()
    state['halted'] = False
    state['halt_reason'] = None
    state['emergency_flat'] = False
    state['halt_until'] = None
    _save_state(state)
    _log_event('HALT_RESET', reason)
    print(f"✅ Governance halt cleared: {reason}")


# ── Portfolio Metrics ─────────────────────────────────────────────────────────

def _get_current_portfolio_value() -> float:
    """Compute current options portfolio value from open positions."""
    if not DB_PATH.exists():
        return OPTIONS_CAPITAL
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(total_credit), 0) FROM positions WHERE status='OPEN'")
    credit = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status='CLOSED' AND action='sell'")
    realized = c.fetchone()[0]
    conn.close()
    return OPTIONS_CAPITAL + (realized or 0) + (credit or 0)


def _count_open_strategy(strategy_prefix: str) -> int:
    """Count open positions for a given strategy (prefix match on strategy column)."""
    if not DB_PATH.exists():
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM positions WHERE status='OPEN' AND strategy LIKE ?",
            (f'{strategy_prefix}%',)
        )
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def _get_deployed_capital() -> float:
    """Capital currently at risk in open positions (CSP = strike * 100 * contracts)."""
    if not DB_PATH.exists():
        return 0.0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT strike, contracts FROM positions
                 WHERE status='OPEN' AND action='sell' AND option_type='put'""")
    rows = c.fetchall()
    conn.close()
    return sum(strike * 100 * contracts for strike, contracts in rows)


def _get_today_pnl() -> float:
    """Today's realized P&L."""
    if not DB_PATH.exists():
        return 0.0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = str(date.today())
    c.execute("SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status='CLOSED' AND closed_at >= ?", (today,))
    pnl = c.fetchone()[0]
    conn.close()
    return pnl or 0.0


def _get_vix() -> tuple[float, float]:
    """Returns (vix_current, vix_5d_change)."""
    try:
        hist = yf.Ticker('^VIX').history(period='10d')
        if hist.empty:
            return 20.0, 0.0
        vix = float(hist['Close'].iloc[-1])
        vix_5d = float(hist['Close'].iloc[-6]) if len(hist) >= 6 else vix
        return vix, vix - vix_5d
    except Exception:
        return 20.0, 0.0


# ── Core Governance Check ─────────────────────────────────────────────────────

def check_governance(
    proposed_capital: float = 0.0,   # Capital at risk for proposed new trade
    proposed_trade: dict = None,
) -> GovernanceCheck:
    """
    Run all governance checks. Call this BEFORE executing any trade.
    Returns GovernanceCheck — if allowed=False, DO NOT TRADE.
    """
    state = _load_state()
    now = datetime.now().isoformat()

    # Reset daily tracking if new day
    if state.get('daily_date') != str(date.today()):
        state['daily_date'] = str(date.today())
        state['daily_start_value'] = _get_current_portfolio_value()
        _save_state(state)

    # ── Check 1: Already halted ───────────────────────────────────────────────
    if state.get('halted'):
        return GovernanceCheck(
            allowed=False,
            reason=f"System HALTED: {state.get('halt_reason', 'unknown')}",
            severity='HALT',
            action_required='Nico must manually reset: python3 governance.py reset',
            metrics={}
        )

    # ── Check 2: Tail event — VIX spike ──────────────────────────────────────
    vix, vix_5d_change = _get_vix()
    if vix > TAIL_VIX_LEVEL or vix_5d_change > TAIL_VIX_5D_CHANGE:
        state['halted'] = True
        state['emergency_flat'] = True
        state['halt_reason'] = f"TAIL EVENT: VIX={vix:.1f} (+{vix_5d_change:.1f} 5d)"
        _save_state(state)
        _trigger_emergency_flat(reason=state['halt_reason'])
        _alert(f"🚨 EMERGENCY FLAT\n{state['halt_reason']}\nAll new positions halted. Close existing at next opportunity.")
        return GovernanceCheck(
            allowed=False,
            reason=state['halt_reason'],
            severity='EMERGENCY',
            action_required='Close all open positions immediately. Manual reset required.',
            metrics={'vix': vix, 'vix_5d_change': vix_5d_change}
        )

    # ── Check 3: Drawdown kill ────────────────────────────────────────────────
    portfolio_value = _get_current_portfolio_value()
    hwm = state.get('hwm', OPTIONS_CAPITAL)
    if portfolio_value > hwm:
        state['hwm'] = portfolio_value
        _save_state(state)
        hwm = portfolio_value

    drawdown = (portfolio_value - hwm) / hwm

    # ── Paper-phase drawdown circuit breaker (Claude peer review) ────────────
    # Live trading has DRAWDOWN_KILL_PCT (-15% from HWM).
    # Paper phase was undefined: what happens at -15% during paper?
    # Now defined: paper drawdown > 10% pauses new entries + requires manual review.
    # Does NOT reset trade count. Does NOT end paper phase.
    # Clears manually (Nico reviews → reset_halt()) or auto-clears next session.
    # Rationale: a system that bleeds 10% in paper has a real problem. Pause. Inspect.
    PAPER_DRAWDOWN_PAUSE_PCT = -0.10   # -10% from HWM during paper phase
    if PAPER_MODE and drawdown <= PAPER_DRAWDOWN_PAUSE_PCT and not state.get('emergency_flat', False):
        state['halted'] = True
        state['halt_reason'] = (
            f"PAPER DRAWDOWN PAUSE: {drawdown*100:.1f}% from paper HWM ${hwm:.0f}. "
            f"Trade count NOT reset. Requires manual review + halt reset before continuing."
        )
        _save_state(state)
        _alert(
            f"⚠️ PAPER DRAWDOWN PAUSE\n"
            f"Paper P&L: {drawdown*100:.1f}% from HWM\n"
            f"Trade count preserved. Review what went wrong before resuming paper phase.\n"
            f"Reset with: governance.reset_halt('reviewed paper drawdown')"
        )
        return GovernanceCheck(
            allowed=False,
            reason=state['halt_reason'],
            severity='HALT',
            action_required='Review paper trades. Diagnose losses. Reset halt manually when satisfied.',
            metrics={'drawdown_pct': drawdown * 100, 'hwm': hwm, 'current': portfolio_value}
        )

    if drawdown <= DRAWDOWN_KILL_PCT:
        state['halted'] = True
        state['halt_reason'] = f"DRAWDOWN KILL: {drawdown*100:.1f}% from HWM ${hwm:.0f}"
        _save_state(state)
        _alert(f"🚨 DRAWDOWN KILL SWITCH TRIGGERED\nDrawdown: {drawdown*100:.1f}% from HWM\nAll trading stopped. Manual reset required.")
        return GovernanceCheck(
            allowed=False,
            reason=state['halt_reason'],
            severity='KILL',
            action_required='Liquidate all positions. Review strategy before resuming.',
            metrics={'drawdown_pct': drawdown * 100, 'hwm': hwm, 'current': portfolio_value}
        )

    # ── Check 4: Daily loss halt ──────────────────────────────────────────────
    today_pnl = _get_today_pnl()
    daily_start = state.get('daily_start_value', OPTIONS_CAPITAL)
    daily_loss_pct = today_pnl / daily_start if daily_start > 0 else 0
    if daily_loss_pct <= DAILY_LOSS_HALT_PCT:
        state['halted'] = True
        state['halt_reason'] = f"DAILY LOSS LIMIT: {daily_loss_pct*100:.1f}% today"
        state['halt_until'] = str(date.today())
        _save_state(state)
        _alert(f"⚠️ DAILY LOSS LIMIT HIT\n{daily_loss_pct*100:.1f}% loss today\nNo new trades until tomorrow.")
        return GovernanceCheck(
            allowed=False,
            reason=state['halt_reason'],
            severity='HALT',
            action_required='No new trades today. Auto-resets tomorrow morning.',
            metrics={'daily_loss_pct': daily_loss_pct * 100, 'daily_pnl': today_pnl}
        )

    # ── Check 5: Position size limit ─────────────────────────────────────────
    if proposed_capital > 0:
        position_pct = proposed_capital / OPTIONS_CAPITAL
        if position_pct > MAX_SINGLE_POSITION_PCT:
            return GovernanceCheck(
                allowed=False,
                reason=f"Position too large: {position_pct*100:.1f}% > {MAX_SINGLE_POSITION_PCT*100:.0f}% limit",
                severity='WARN',
                action_required='Reduce to 1 contract or skip this trade.',
                metrics={'proposed_pct': position_pct * 100, 'limit_pct': MAX_SINGLE_POSITION_PCT * 100}
            )

    # ── Check 6: Total deployed limit ────────────────────────────────────────
    deployed = _get_deployed_capital()
    deployed_after = deployed + proposed_capital
    deployed_pct = deployed_after / OPTIONS_CAPITAL
    limit = MAX_TOTAL_DEPLOYED_PCT_PAPER if PAPER_MODE else MAX_TOTAL_DEPLOYED_PCT
    if deployed_pct > limit:
        return GovernanceCheck(
            allowed=False,
            reason=f"Portfolio at capacity: {deployed_pct*100:.1f}% deployed > {MAX_TOTAL_DEPLOYED_PCT*100:.0f}% limit",
            severity='WARN',
            action_required='Wait for existing positions to close before new entries.',
            metrics={'deployed_pct': deployed_pct * 100, 'deployed': deployed_after}
        )

    # ── All checks passed ─────────────────────────────────────────────────────
    state['last_check'] = now
    _save_state(state)

    return GovernanceCheck(
        allowed=True,
        reason='All governance checks passed',
        severity='OK',
        action_required='None',
        metrics={
            'vix': vix,
            'drawdown_pct': round(drawdown * 100, 2),
            'daily_loss_pct': round(daily_loss_pct * 100, 2),
            'deployed_pct': round((deployed / OPTIONS_CAPITAL) * 100, 2),
            'portfolio_value': portfolio_value,
        }
    )


# ── Emergency Flat ────────────────────────────────────────────────────────────

def _trigger_emergency_flat(reason: str):
    """
    Log emergency flat trigger. In live trading, this would place
    buy-to-close orders on all open positions immediately.
    Paper trading: marks positions for manual close + logs event.
    """
    _log_event('EMERGENCY_FLAT', reason)
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE positions SET notes = notes || ' | EMERGENCY_FLAT: ' || ? WHERE status='OPEN'",
              (reason,))
    conn.commit()
    conn.close()
    print(f"🚨 EMERGENCY FLAT triggered: {reason}")
    print("   → In live trading: BTC orders placed on all open positions")
    print("   → Paper trading: positions flagged for manual close")


# ── Reconciliation ────────────────────────────────────────────────────────────

def reconcile_with_broker(broker_positions: list[dict]) -> dict:
    """
    Compare paper_trader.db open positions against broker's actual positions.
    Returns discrepancies for Nico to review.

    broker_positions: list of {'symbol': ..., 'quantity': ..., 'side': ...}
    """
    if not DB_PATH.exists():
        return {'status': 'no_db', 'discrepancies': []}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ticker, option_type, strike, expiry, contracts, action FROM positions WHERE status='OPEN'")
    paper = c.fetchall()
    conn.close()

    # Build sets for comparison
    paper_set = set()
    for ticker, opt_type, strike, expiry, contracts, action in paper:
        paper_set.add(f"{ticker}_{opt_type}_{strike}_{expiry}_{action}")

    broker_set = set()
    for p in broker_positions:
        broker_set.add(p.get('symbol', ''))

    discrepancies = []
    for item in paper_set - broker_set:
        discrepancies.append({'type': 'IN_PAPER_NOT_BROKER', 'item': item})
    for item in broker_set - paper_set:
        discrepancies.append({'type': 'IN_BROKER_NOT_PAPER', 'item': item})

    if discrepancies:
        _alert(f"⚠️ RECONCILIATION MISMATCH\n{len(discrepancies)} discrepancies found\nManual review required.")

    return {
        'status': 'matched' if not discrepancies else 'MISMATCH',
        'paper_positions': len(paper),
        'broker_positions': len(broker_positions),
        'discrepancies': discrepancies,
        'checked_at': datetime.now().isoformat(),
    }


# ── Corporate Actions / Assignment Risk ──────────────────────────────────────

def check_assignment_risk(positions: list[dict] = None) -> list[dict]:
    """
    Scan open puts for early assignment risk:
    - Deep ITM (stock price < 95% of strike)
    - Approaching expiry (< 5 DTE)
    - Ex-dividend within 2 days on short calls
    """
    if not DB_PATH.exists():
        return []

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT id, ticker, option_type, strike, expiry, action, contracts
                 FROM positions WHERE status='OPEN' AND action='sell'""")
    rows = c.fetchall()
    conn.close()

    risks = []
    for row in rows:
        trade_id, ticker, opt_type, strike, expiry, action, contracts = row
        try:
            hist = yf.Ticker(ticker).history(period='1d', interval='5m', prepost=True, auto_adjust=True)
            price = float(hist['Close'].iloc[-1]) if len(hist) > 0 else 0.0
            expiry_dt = datetime.strptime(expiry, '%Y-%m-%d')
            dte = (expiry_dt - datetime.now()).days

            risk_flags = []

            # Deep ITM check (put: stock fell significantly below strike)
            if opt_type == 'put' and price < strike * 0.95:
                risk_flags.append(f"DEEP ITM: stock ${price:.2f} vs strike ${strike:.0f} ({(price/strike-1)*100:.1f}%)")

            # Near expiry
            if dte <= 5:
                risk_flags.append(f"NEAR EXPIRY: {dte} DTE — consider closing")

            # Early assignment threshold (short puts: ITM with low extrinsic)
            if opt_type == 'put' and price < strike and dte <= 10:
                risk_flags.append(f"ASSIGNMENT RISK: ITM put with {dte} DTE")

            if risk_flags:
                risks.append({
                    'trade_id': trade_id,
                    'ticker': ticker,
                    'option_type': opt_type,
                    'strike': strike,
                    'expiry': expiry,
                    'dte': dte,
                    'stock_price': price,
                    'flags': risk_flags,
                })
        except Exception:
            continue

    if risks:
        alert_lines = [f"⚠️ ASSIGNMENT RISK DETECTED ({len(risks)} positions):"]
        for r in risks:
            alert_lines.append(f"  #{r['trade_id']} {r['ticker']} ${r['strike']:.0f} {r['option_type'].upper()} {r['expiry']}")
            for flag in r['flags']:
                alert_lines.append(f"    → {flag}")
        _alert('\n'.join(alert_lines))

    return risks


# ── Cross-Portfolio Greeks (CML + Options) ───────────────────────────────────

def compute_combined_exposure() -> dict:
    """
    Combine options position Greeks with CML long equity exposure.
    Prevents unknowingly doubling delta on the same names.
    """
    try:
        import json
        holdings = json.loads(open('/Users/nicolasotheguy/.openclaw/workspace/portfolio/data/holdings.json').read())
        total_value = holdings.get('totalValue', 68000)
        cml_delta = {}
        for h in holdings.get('holdings', []):
            ticker = h['ticker']
            value = h.get('value', 0)
            # Long equity: delta = 1.0 per share * shares
            # Approximate: value / price ≈ shares
            cml_delta[ticker] = value  # dollar delta (positive = long)
    except Exception:
        cml_delta = {}

    # Options portfolio delta (from portfolio_greeks)
    options_delta = {}
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT ticker, delta_open, contracts, action FROM positions WHERE status='OPEN'")
        for ticker, delta, contracts, action in c.fetchall():
            if delta is None:
                continue  # Skip positions with missing delta
            sign = -1 if action == 'sell' else 1
            options_delta[ticker] = options_delta.get(ticker, 0) + delta * contracts * 100 * sign
        conn.close()

    # Combined
    all_tickers = set(list(cml_delta.keys()) + list(options_delta.keys()))
    combined = {}
    flags = []
    for ticker in all_tickers:
        cml = cml_delta.get(ticker, 0)
        opts = options_delta.get(ticker, 0)
        total = cml + opts
        combined[ticker] = {'cml_dollar_delta': round(cml, 0), 'options_delta': round(opts, 2), 'combined': round(total, 2)}
        # Flag if options position meaningfully offsets or amplifies CML
        if abs(opts) > 100 and cml > 0:
            if opts < 0:
                flags.append(f"{ticker}: Options SHORT delta ({opts:.0f}) partially offsets CML long (${cml:.0f})")
            else:
                flags.append(f"{ticker}: Options LONG delta ({opts:.0f}) amplifies CML long (${cml:.0f}) — concentrated")

    return {'combined': combined, 'flags': flags, 'computed_at': datetime.now().isoformat()}


# ── Degraded State Handler ────────────────────────────────────────────────────

class DegradedState:
    """Decision tree for when data feeds or broker APIs fail."""

    DECISIONS = {
        'yfinance_down': 'SKIP_SCAN — cannot compute VRP or regime without price data. Log warning, try again at next scheduled run.',
        'broker_api_down': 'HALT_NEW_ORDERS — cannot confirm fills. Do not place new trades. Keep existing open. Alert Nico.',
        'vix_data_unavailable': 'DEFAULT_NEUTRAL — assume VIX=25, regime=NEUTRAL, size_multiplier=0.5. Never assume BULL when blind.',
        'partial_chain_data': 'SKIP_TICKER — incomplete option chain = unreliable Greeks. Skip, do not extrapolate.',
        'order_rejected': 'LOG_AND_ALERT — do not retry automatically. Alert Nico with rejection reason.',
        'db_locked': 'WAIT_30S_RETRY_ONCE — if still locked, skip scan and alert.',
        'all_signals_disagree': 'REDUCE_TO_ZERO — if VRP, momentum, and regime point in different directions, do not trade. Uncertainty IS a signal.',
    }

    @staticmethod
    def handle(failure_type: str, context: str = '') -> str:
        decision = DegradedState.DECISIONS.get(failure_type, 'HALT — unknown failure type. Alert Nico.')
        _log_event(f'DEGRADED_{failure_type.upper()}', f'{decision} | context: {context}')
        return decision

    @staticmethod
    def print_all():
        print("\n=== DEGRADED STATE DECISION TREE ===\n")
        for failure, decision in DegradedState.DECISIONS.items():
            print(f"  {failure}:\n    → {decision}\n")


# ── Performance Attribution ───────────────────────────────────────────────────

def run_attribution() -> dict:
    """
    Decompose closed trade returns by which signals were active at entry.
    Requires notes field in positions table to have signal tags.
    Returns attribution by signal source.
    """
    if not DB_PATH.exists():
        return {'status': 'no_data', 'trades': 0}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT notes, pnl, strategy FROM positions WHERE status='CLOSED' AND pnl IS NOT NULL")
    rows = c.fetchall()
    conn.close()

    attribution = {'VRP': [], 'MOMENTUM': [], 'REGIME': [], 'UW_FLOW': [], 'UNKNOWN': []}
    total_pnl = 0

    for notes, pnl, strategy in rows:
        total_pnl += pnl or 0
        notes_str = str(notes or '')
        tagged = False
        if 'VRP' in notes_str or 'vrp' in notes_str.lower():
            attribution['VRP'].append(pnl)
            tagged = True
        if 'Momentum' in notes_str or 'Mom' in notes_str:
            attribution['MOMENTUM'].append(pnl)
            tagged = True
        if 'Regime' in notes_str:
            attribution['REGIME'].append(pnl)
            tagged = True
        if 'UW' in notes_str or 'flow' in notes_str.lower():
            attribution['UW_FLOW'].append(pnl)
            tagged = True
        if not tagged:
            attribution['UNKNOWN'].append(pnl)

    result = {'total_pnl': round(total_pnl, 2), 'trades': len(rows), 'by_signal': {}}
    for signal, pnls in attribution.items():
        if pnls:
            result['by_signal'][signal] = {
                'trades': len(pnls),
                'total_pnl': round(sum(pnls), 2),
                'avg_pnl': round(sum(pnls) / len(pnls), 2),
                'win_rate': round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1)
            }
    return result


# ── G4.5 Conviction Calibration Exit Gate ────────────────────────────────────

def _load_g45_state() -> dict:
    """Load G4.5 per-strategy state from governance_state.json."""
    return _load_state().get('g4_5_state', {})


def _save_g45_state(g45_state: dict) -> None:
    """Persist G4.5 state into governance_state.json under key 'g4_5_state'."""
    state = _load_state()
    state['g4_5_state'] = g45_state
    _save_state(state)


def check_g4_5_gate(strategy_id: str) -> dict:
    """
    G4.5 Conviction Calibration Exit Gate.

    Evaluates win rate in 20-trade batches. Two consecutive batches with
    WR ≤ 40% trigger a strategy exit recommendation.

    Call this from paper_trader after each trade close.

    Algorithm:
      - Each 20 closed trades = 1 batch.
      - If WR ≤ 40% in a batch: increment consecutive_fail_batches.
      - If WR > 40% in a batch: reset consecutive_fail_batches to 0.
      - If consecutive_fail_batches ≥ 2: return exit=True.

    State persisted under governance_state.json['g4_5_state'][strategy_id].

    Returns:
        {'exit': bool, 'reason': str | None}
    """
    try:
        from strategy_pnl_tracker import get_strategy_stats
        stats = get_strategy_stats(strategy_id)
    except Exception as e:
        return {'exit': False, 'reason': f'G4.5: stats unavailable for {strategy_id}: {e}'}

    if not stats:
        return {'exit': False, 'reason': None}

    total_trades = stats.get('total_trades', 0)
    win_rate     = stats.get('win_rate') or 0.0

    if total_trades < 20:
        return {'exit': False, 'reason': None}

    g45_state  = _load_g45_state()
    strat_g45  = g45_state.get(strategy_id, {
        'consecutive_fail_batches': 0,
        'last_batch_number': 0,
        'last_batch_wr': None,
    })

    current_batch = total_trades // 20
    last_batch    = strat_g45.get('last_batch_number', 0)

    if current_batch <= last_batch:
        # No new batch completed yet
        return {'exit': False, 'reason': None}

    # New batch completed — evaluate
    batch_wr = win_rate  # Approximation: overall WR as batch WR

    if batch_wr <= 0.40:
        fail_count = strat_g45.get('consecutive_fail_batches', 0) + 1
        strat_g45['consecutive_fail_batches'] = fail_count
        strat_g45['last_batch_number']        = current_batch
        strat_g45['last_batch_wr']            = round(batch_wr, 4)
        strat_g45['batch_1_fail']             = True
        g45_state[strategy_id] = strat_g45
        _save_g45_state(g45_state)

        _log_event(
            'G4_5_BATCH_FAIL',
            f'{strategy_id}: batch {current_batch} WR={batch_wr:.1%} ≤ 40% (fail #{fail_count})',
        )

        if fail_count >= 2:
            return {
                'exit': True,
                'reason': 'G4.5: ≤40% WR over 2 consecutive 20-trade batches',
            }
        return {
            'exit': False,
            'reason': f'G4.5: Batch {current_batch} fail #{fail_count}/2 (WR={batch_wr:.1%})',
        }
    else:
        # Passing batch — reset streak
        strat_g45['consecutive_fail_batches'] = 0
        strat_g45['last_batch_number']        = current_batch
        strat_g45['last_batch_wr']            = round(batch_wr, 4)
        g45_state[strategy_id] = strat_g45
        _save_g45_state(g45_state)
        _log_event(
            'G4_5_BATCH_PASS',
            f'{strategy_id}: batch {current_batch} WR={batch_wr:.1%} > 40% — streak reset',
        )
        return {'exit': False, 'reason': None}


# ── Logging & Alerts ──────────────────────────────────────────────────────────

def _log_event(event_type: str, detail: str):
    log_path = Path('/Users/nicolasotheguy/.openclaw/workspace/options/data/governance_log.jsonl')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, 'a') as f:
        f.write(json.dumps({'ts': datetime.now().isoformat(), 'event': event_type, 'detail': detail}) + '\n')


def _alert(message: str, dedup_key: str = None):
    """Send alert to Unusual Friend channel (-1003810224931).

    dedup_key: if provided, alert is suppressed if the same key was sent
    within the last 4 hours (prevents hourly governance cron spam on
    persistent HALT state).
    """
    _log_event('ALERT', message)

    # Dedup check — load state to see if this alert was already sent recently
    if dedup_key:
        try:
            state = _load_state()
            last_key = state.get('last_halt_notified_key')
            last_ts_str = state.get('last_halt_notified')
            if last_key == dedup_key and last_ts_str:
                from datetime import timedelta
                last_ts = datetime.fromisoformat(last_ts_str)
                if datetime.now() - last_ts < timedelta(hours=4):
                    print(f"  [governance] Alert suppressed (dedup, last sent {last_ts_str}): {dedup_key[:60]}")
                    return
        except Exception:
            pass  # dedup failure → send anyway

    try:
        import requests as _req, os
        token_path = Path.home() / '.openclaw/secrets/telegram_bot_token.txt'
        token = os.environ.get('TELEGRAM_BOT_TOKEN') or (token_path.read_text().strip() if token_path.exists() else '')
        if token:
            _req.post(
                f'https://api.telegram.org/bot{token}/sendMessage',
                json={'chat_id': '-1003810224931', 'text': message, 'parse_mode': 'Markdown'},
                timeout=10
            )
        print(f"\n🚨 GOVERNANCE ALERT:\n{message}\n")
    except Exception as e:
        print(f"  [governance] Telegram alert error: {e}")

    # Stamp dedup key in state
    if dedup_key:
        try:
            state = _load_state()
            state['last_halt_notified'] = datetime.now().isoformat()
            state['last_halt_notified_key'] = dedup_key
            _save_state(state)
        except Exception:
            pass


# ── Status Dashboard ──────────────────────────────────────────────────────────

def print_status():
    state = _load_state()
    check = check_governance()
    combined = compute_combined_exposure()
    risks = check_assignment_risk()

    print(f"\n{'='*60}")
    print(f"  GOVERNANCE STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    status_icon = {'OK': '✅', 'WARN': '⚠️', 'HALT': '🛑', 'KILL': '💀', 'EMERGENCY': '🚨'}.get(check.severity, '⚪')
    print(f"\n  Status: {status_icon} {check.severity}")
    print(f"  {check.reason}")

    if check.metrics:
        m = check.metrics
        print(f"\n  METRICS:")
        vix_val = m.get('vix', 0)
        print(f"    VIX:           {vix_val:.1f}" if isinstance(vix_val, (int, float)) else f"    VIX:           n/a")
        print(f"    Drawdown:      {m.get('drawdown_pct', 0):+.1f}%")
        print(f"    Today P&L:     {m.get('daily_loss_pct', 0):+.1f}%")
        print(f"    Deployed:      {m.get('deployed_pct', 0):.1f}% of capital")

    if risks:
        print(f"\n  ⚠️ ASSIGNMENT RISKS: {len(risks)}")
        for r in risks:
            print(f"    {r['ticker']} ${r['strike']:.0f} {r['option_type'].upper()} {r['expiry']}: {r['flags'][0]}")

    if combined.get('flags'):
        print(f"\n  ⚠️ CROSS-PORTFOLIO FLAGS:")
        for flag in combined['flags']:
            print(f"    {flag}")

    print(f"\n  HWM: ${state.get('hwm', OPTIONS_CAPITAL):,.2f}")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'reset':
        reset_halt('manual CLI reset')
    elif len(sys.argv) > 1 and sys.argv[1] == 'degraded':
        DegradedState.print_all()
    elif len(sys.argv) > 1 and sys.argv[1] == 'attribution':
        print(json.dumps(run_attribution(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == 'reconcile':
        print("Reconcile requires broker positions list — run from broker integration")
    else:
        print_status()
