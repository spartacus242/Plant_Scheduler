# Historical run log parse summary

- source: `npa_historical_run_log.csv`  rows: 12006
- date range: 2018-01-02 .. 2026-09-02
- usable_default: 11952  (unusable when any of: date_unparsed, oee_not_numeric, oee_gt_1, hrs_zero, pouches_zero)
- rows with a review flag: 1909

## Review flags (possible data errors)

| flag | rows |
|---|---|
| date_unparsed | 0 |
| oee_not_numeric | 21 |
| oee_zero | 4 |
| oee_low | 90 |
| oee_gt_1 | 29 |
| hrs_zero | 15 |
| hrs_long | 25 |
| hrs_overlaps_next_mo | 535 |
| mo_multi_sku_keying_error | 958 |
| mo_out_of_sequence | 127 |
| dup_date_line_sku | 386 |
| pouches_zero | 13 |
| pouch_weight_missing | 4 |
| rate_mismatch_vs_source | 0 |
| oee_inconsistent_with_rate | 103 |

## Info flags (not errors)

| flag | rows |
|---|---|
| mo_split_across_lines | 478 |
| sku_not_in_master | 3592 |
| pair_not_in_capability_table | 3577 |
| pair_not_flagged_capable | 440 |

## Rows per year

| iso_year | rows | usable |
|---|---|---|
| 2018 | 963 | 957 |
| 2019 | 1196 | 1187 |
| 2020 | 1332 | 1322 |
| 2021 | 1299 | 1298 |
| 2022 | 1428 | 1423 |
| 2023 | 1325 | 1317 |
| 2024 | 1626 | 1623 |
| 2025 | 1564 | 1556 |
| 2026 | 1273 | 1269 |
