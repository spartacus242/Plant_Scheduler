# Derived historical tables

- built from `historical_run_log_clean.csv` (last run 2026-09-02); recent window from 2024-09-02 (2 y)
- usable runs: 11948 (recent 3344); runs under 1.0 h ignored for rate/campaign stats

| table | rows |
|---|---|
| rates_by_line_sku.csv | 887 |
| rates_by_line.csv | 14 |
| rates_by_sku.csv | 232 |
| campaign_norms.csv | 232 |
| line_sku_affinity.csv | 887 |
| transitions.csv | 5429 |
| weekly_line_hours.csv | 3954 |

## Limitations

- Changeover duration is NOT measurable from this log: MO start time-of-day is not recorded, so `transitions.csv` carries counts only. Timestamped run data would be needed for durations.
- `rates_by_line_sku.csv` compares against `calc_rate_kgph` from the live capabilities table (double lines summed across A/B). `low_evidence` pairs have fewer than 3 usable runs all-time.
- Recent columns are empty (NaN) where a pair or SKU has not run inside the recent window.
