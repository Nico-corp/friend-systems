# Options Strategy Autoresearch Program

Optimize options strategy parameters to maximize paper trade win rate and P&L. Adjust strike deltas, DTE windows, and conviction thresholds. Do not change risk limits (max loss, spread width hard caps). Prefer conservative changes — small adjustments to one parameter at a time.

## Scoring
Score = win_rate * 0.6 + avg_pnl_pct * 0.4

## Constraints
- CSP_STRIKE_DELTA must stay between 0.15 and 0.45
- MIN_DAYS_TO_EXPIRY must be < MAX_DAYS_TO_EXPIRY
- BULL_PUT_SPREAD_WIDTH is a hard cap — do not increase above 10
- Only change one or two parameters per experiment
