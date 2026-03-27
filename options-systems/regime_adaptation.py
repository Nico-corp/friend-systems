#!/usr/bin/env python3
"""
Regime Adaptation Logic (Agent Strength Initiative #2)

Auto-shifts strategy mix + capital scaling when regime changes.
VIX > 25 (BEAR): disable S002/S006, reduce sizing 30%, tighten SL 20%
VIX < 18 (BULL): re-enable, scale sizing normal, reset SL
VIX 18-25 (NEUTRAL): normal operations

Wired into options_desk.py entry layer.
"""

import json
import yfinance as yf
from pathlib import Path
from datetime import datetime

CONFIG_PATH = Path.home() / ".openclaw/workspace/options/config/regime_config.json"

REGIME_THRESHOLDS = {
    "BEAR": {"vix_min": 25, "size_multiplier": 0.7, "sl_tighten": 1.2},
    "NEUTRAL": {"vix_min": 18, "vix_max": 25, "size_multiplier": 1.0, "sl_tighten": 1.0},
    "BULL": {"vix_max": 18, "size_multiplier": 1.3, "sl_tighten": 0.9}
}

def get_vix():
    """Fetch current VIX."""
    try:
        vix = yf.download('^VIX', period='1d', progress=False)['Close']
        return float(vix.iloc[-1]) if len(vix) > 0 else 20.0
    except:
        return 20.0  # Default to NEUTRAL

def detect_regime(vix=None):
    """
    v6.6 thin wrapper → delegates to canonical get_regime() in signals/regime.py.
    Returns a dict compatible with the old shape plus full v6.6 fields.

    Legacy callers that only use ["state"], ["vix"], ["size_multiplier"],
    ["enabled_strategies"] will continue to work without changes.
    """
    try:
        from signals.regime import get_regime as _get_regime
        r = _get_regime()
        # Build sl_tighten for legacy callers
        sl_map = {"BULL": 0.9, "NEUTRAL": 1.0, "BEAR": 1.2, "EXTREME": 1.3, "TRANSITION": 1.5}
        state = r["state"]
        warning = None
        if state == "BEAR":
            warning = "🔴 BEAR REGIME: short-vol strategies disabled, sizing cut 30%"
        elif state == "EXTREME":
            warning = "🚨 EXTREME REGIME: VIX ≥ 35 — only S005 enabled, sizing 50%"
        elif state == "TRANSITION":
            warning = f"⚠️ TRANSITION: {r.get('transition_reason', 'regime unstable')} — no new entries"
        elif state == "BULL":
            warning = "🟢 BULL REGIME: All strategies enabled, sizing up 30%"
        return {
            "state": state,
            "vix": r["vix"],
            "rv_ratio": r["rv_ratio"],
            "size_multiplier": r["size_multiplier"],
            "sl_tighten": sl_map.get(state, 1.0),
            "enabled_strategies": r["enabled_strategies"],
            "transition_reason": r.get("transition_reason"),
            "warning": warning,
            "timestamp": r["timestamp"],
        }
    except Exception as _e:
        # Fallback to original local logic if signals.regime unavailable
        if vix is None:
            vix = get_vix()
        if vix > 25:
            regime = "BEAR"
        elif vix < 18:
            regime = "BULL"
        else:
            regime = "NEUTRAL"
        config = REGIME_THRESHOLDS[regime]
        warning = None
        if regime == "BEAR":
            warning = "🔴 BEAR REGIME: S002/S006/S007 disabled, sizing cut 30%, SL tightened 20%"
        elif regime == "BULL":
            warning = "🟢 BULL REGIME: All strategies enabled, sizing up 30%, SL relaxed"
        return {
            "state": regime,
            "vix": round(vix, 1),
            "size_multiplier": config["size_multiplier"],
            "sl_tighten": config["sl_tighten"],
            "enabled_strategies": get_enabled_strategies(regime),
            "warning": warning,
            "timestamp": datetime.now().isoformat(),
        }

def get_enabled_strategies(regime):
    """Return which strategies are enabled in this regime."""
    enabled = {
        "S002": regime != "BEAR",                          # CSP disabled in BEAR
        "S003": regime != "BEAR",                          # Earnings disabled in BEAR
        "S004": regime == "BEAR",                          # Long gamma ONLY in BEAR
        "S005": regime == "BEAR",                          # Capitulation ONLY in BEAR
        "S006": regime != "BEAR",                          # Iron condor disabled in BEAR
        "S007": regime != "BEAR",                          # PMCC disabled in BEAR
        "S009": regime in ("BULL", "NEUTRAL"),             # VRP strangle: BULL + NEUTRAL only
        "S010": regime in ("BULL", "NEUTRAL"),             # 0DTE IC: BULL + NEUTRAL only (also needs VIX<20 internally)
        "S011": True,                                      # UW tailing always enabled
        # S015: Long Vol Hedge — EV-NEGATIVE. Activation handled internally
        # (LATE_BULL proxy or inverted term structure). Enabled here for NEUTRAL/BULL
        # so governance.validate_entry() does not regime-block it; strategy_015.py
        # applies the fine-grained LATE_BULL / inverted-TS checks.
        "S015": regime in ("BULL", "NEUTRAL"),
    }
    return [k for k, v in enabled.items() if v]

def apply_regime_to_position(strategy, size, sl_pct, regime_state):
    """
    Adjust position sizing and SL based on regime.
    
    Args:
        strategy: "S002", "S003", etc.
        size: position size in dollars
        sl_pct: stop loss percentage (e.g., -15 for -15%)
        regime_state: output from detect_regime()
    
    Returns: {"size_adjusted": X, "sl_adjusted": X, "allowed": bool, "reason": "..."}
    """
    regime = regime_state["state"]
    multiplier = regime_state["size_multiplier"]
    sl_tighten = regime_state["sl_tighten"]
    
    # Check if strategy is enabled
    enabled_strats = regime_state["enabled_strategies"]
    if strategy not in enabled_strats:
        return {
            "size_adjusted": 0,
            "sl_adjusted": sl_pct,
            "allowed": False,
            "reason": f"{strategy} disabled in {regime} regime"
        }
    
    # Apply sizing multiplier
    size_adjusted = size * multiplier
    
    # Apply SL tightening (makes SL worse/tighter when sl_tighten > 1)
    sl_adjusted = sl_pct * sl_tighten
    
    return {
        "size_adjusted": round(size_adjusted, 2),
        "sl_adjusted": round(sl_adjusted, 2),
        "allowed": True,
        "reason": f"{regime} regime: size scaled {multiplier}x, SL adjusted {sl_tighten}x"
    }

def save_regime_state(regime_state):
    """Persist current regime state to config."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(regime_state, f, indent=2)

def load_regime_state():
    """Load last known regime state."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return None

def check_regime_transition(last_regime=None):
    """
    Check if regime has changed since last check.
    Alert Nico if transition detected.
    
    Returns: {"transitioned": bool, "from": old_regime, "to": new_regime, "alert": "..."}
    """
    current = detect_regime()
    
    if last_regime is None:
        last = load_regime_state()
        last_regime = last.get("state") if last else None
    
    if last_regime is None or last_regime == current["state"]:
        return {"transitioned": False, "current": current["state"]}
    
    alert = f"⚠️ REGIME TRANSITION: {last_regime} → {current['state']} (VIX {current['vix']})\n"
    alert += f"Strategy mix: {current['enabled_strategies']}\n"
    alert += f"Sizing: {current['size_multiplier']}x | SL: {current['sl_tighten']}x"
    
    save_regime_state(current)
    
    return {
        "transitioned": True,
        "from": last_regime,
        "to": current["state"],
        "alert": alert,
        "regime_state": current
    }

if __name__ == "__main__":
    regime = detect_regime()
    print(json.dumps(regime, indent=2))
