#!/usr/bin/env python3
"""
Execution Discipline Monitor (Agent Strength Initiative #5)

Pre-execution validator: ensures regime gate, capital, size, and SL are ALL correct.
Post-execution validator: enforces TP (50%+) / SL (-15%) / time-based exits.

Wired into options_desk.py entry/exit layer.
Blocks or alerts on ALL constraint violations.
"""

import json
from datetime import datetime
from pathlib import Path

VIOLATIONS_LOG = Path.home() / ".openclaw/workspace/options/logs/execution_violations.jsonl"

def init_log():
    """Ensure violations log exists."""
    VIOLATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    VIOLATIONS_LOG.touch(exist_ok=True)

def log_violation(violation_type, strategy, ticker, reason, constraint_name, resolved=False):
    """Log an execution constraint violation."""
    init_log()
    
    entry = {
        "timestamp": datetime.now().isoformat(),
        "violation_type": violation_type,
        "strategy": strategy,
        "ticker": ticker,
        "reason": reason,
        "constraint": constraint_name,
        "resolved": resolved
    }
    
    with open(VIOLATIONS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

class PreExecutionValidator:
    """
    Validate BEFORE entry.
    All 4 checks must pass for entry.
    """
    
    def __init__(self, strategy, ticker, size, sl_pct, regime_state, capital_available, conviction_score):
        self.strategy = strategy
        self.ticker = ticker
        self.size = size
        self.sl_pct = sl_pct
        self.regime_state = regime_state
        self.capital_available = capital_available
        self.conviction_score = conviction_score
        self.violations = []
    
    def check_regime_gate(self):
        """Check 1: Is regime gate satisfied?"""
        enabled_strategies = self.regime_state.get("enabled_strategies", [])
        
        if self.strategy not in enabled_strategies:
            violation = {
                "check": "regime_gate",
                "passed": False,
                "reason": f"{self.strategy} disabled in {self.regime_state['state']} regime",
                "severity": "HARD_BLOCK"
            }
            self.violations.append(violation)
            log_violation("ENTRY_BLOCKED", self.strategy, self.ticker, violation["reason"], "regime_gate")
            return False
        
        return True
    
    def check_capital_available(self):
        """Check 2: Is capital available?"""
        if self.capital_available < self.size:
            violation = {
                "check": "capital_available",
                "passed": False,
                "reason": f"Need ${self.size}, have ${self.capital_available}",
                "severity": "HARD_BLOCK"
            }
            self.violations.append(violation)
            log_violation("ENTRY_BLOCKED", self.strategy, self.ticker, violation["reason"], "capital")
            return False
        
        return True
    
    def check_position_size(self):
        """Check 3: Is position size correct per capital allocation?"""
        max_per_position = 2500 if self.strategy == "S007" else (4000 if self.strategy == "S002" else 1000)
        
        if self.size > max_per_position:
            violation = {
                "check": "position_size",
                "passed": False,
                "reason": f"Size ${self.size} exceeds max ${max_per_position} for {self.strategy}",
                "severity": "HARD_BLOCK"
            }
            self.violations.append(violation)
            log_violation("ENTRY_BLOCKED", self.strategy, self.ticker, violation["reason"], "position_size")
            return False
        
        return True
    
    def check_sl_configured(self):
        """Check 4: Is SL set correctly per strategy?"""
        strategy_sl_rules = {
            "S011": -15,
            "S002": -20,
            "S003": -30,
            "S006": -25,
            "S007": -30
        }
        
        expected_sl = strategy_sl_rules.get(self.strategy, -20)
        tolerance = 5  # Allow ±5% variance
        
        if not (expected_sl - tolerance <= self.sl_pct <= expected_sl + tolerance):
            violation = {
                "check": "sl_configured",
                "passed": False,
                "reason": f"SL {self.sl_pct}% doesn't match {self.strategy} rule ({expected_sl}%)",
                "severity": "HARD_BLOCK"
            }
            self.violations.append(violation)
            log_violation("ENTRY_BLOCKED", self.strategy, self.ticker, violation["reason"], "sl_configured")
            return False
        
        return True
    
    def validate(self):
        """Run all checks. Return True if ALL pass."""
        checks = [
            self.check_regime_gate(),
            self.check_capital_available(),
            self.check_position_size(),
            self.check_sl_configured()
        ]
        
        all_pass = all(checks)
        
        return {
            "approved": all_pass,
            "violations": self.violations,
            "message": "APPROVED FOR ENTRY" if all_pass else f"ENTRY BLOCKED: {len(self.violations)} violation(s)"
        }

class PostExecutionValidator:
    """
    Validate AT exit.
    TP: 50%+ profit → CLOSE immediately
    SL: -15% loss → CLOSE immediately
    Otherwise: HOLD
    """
    
    def __init__(self, position_id, entry_price, current_price, hold_seconds, strategy):
        self.position_id = position_id
        self.entry_price = entry_price
        self.current_price = current_price
        self.hold_seconds = hold_seconds
        self.strategy = strategy
        self.return_pct = ((current_price - entry_price) / entry_price * 100) if entry_price else 0
    
    def check_take_profit(self):
        """Is this a TP (50%+ profit)?"""
        if self.return_pct >= 50:
            return {
                "action": "CLOSE",
                "reason": f"TP triggered: {self.return_pct:.1f}% profit",
                "exit_type": "TAKE_PROFIT"
            }
        return None
    
    def check_stop_loss(self):
        """Is this a SL (-15% loss)?"""
        if self.return_pct <= -15:
            return {
                "action": "CLOSE",
                "reason": f"SL triggered: {self.return_pct:.1f}% loss",
                "exit_type": "STOP_LOSS"
            }
        return None
    
    def check_time_based_exit(self):
        """Is this a time-based exit (strategy-specific)? Generally N/A."""
        # Override in specific strategy implementations
        return None
    
    def validate(self):
        """Check exit rules. Return action."""
        tp_check = self.check_take_profit()
        if tp_check:
            return tp_check
        
        sl_check = self.check_stop_loss()
        if sl_check:
            return sl_check
        
        time_check = self.check_time_based_exit()
        if time_check:
            return time_check
        
        return {
            "action": "HOLD",
            "reason": f"No exit condition met ({self.return_pct:.1f}% return)",
            "exit_type": "NO_ACTION"
        }

def validate_entry(strategy, ticker, size, sl_pct, regime_state, capital_available, conviction_score):
    """Entry point: validate and return approval."""
    validator = PreExecutionValidator(
        strategy=strategy,
        ticker=ticker,
        size=size,
        sl_pct=sl_pct,
        regime_state=regime_state,
        capital_available=capital_available,
        conviction_score=conviction_score
    )
    return validator.validate()

def validate_exit(position_id, entry_price, current_price, hold_seconds, strategy):
    """Exit point: determine action."""
    validator = PostExecutionValidator(
        position_id=position_id,
        entry_price=entry_price,
        current_price=current_price,
        hold_seconds=hold_seconds,
        strategy=strategy
    )
    return validator.validate()

if __name__ == "__main__":
    init_log()
    print("✅ Execution discipline monitor ready")
