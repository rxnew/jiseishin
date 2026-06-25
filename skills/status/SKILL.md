---
name: status
description: Check jiseishin's cost, limit, and remaining budget. Use this when the user wants to know "how much did I spend today", the "cost", "spend", "how much is left until the limit", or "usage status". Specify a date (e.g. "yesterday", "6/18") to check that day's figures too. Does not change the configuration.
argument-hint: "[date]"
---

# Checking jiseishin usage status

Display the jiseishin plugin's "cost for a given day (today by default), converted to USD at standard API rates" and the current limit (read-only; does not change the configuration).

## Steps

1. Determine the target day.
   - If no date is given, target **today** (run with no arguments).
   - Convert relative or shorthand notations like "yesterday", "last Monday", or "6/18" into an **absolute date in `YYYY-MM-DD` format** before passing it (the script only accepts `YYYY-MM-DD`).
2. Run the following command and report the result (date, spend / limit, percentage, source, rate type) to the user:

   ```bash
   # today (default)
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" status

   # specify a date
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" status 2026-06-18
   ```

## Example output

```
date          : 2026-06-18
total cost    : $12.34 / $100.00 (12.3%)
source        : default $100.00
rates         : Anthropic standard API rates (USD/MTok; batch/priority/subscription not supported)
```

## Notes

- The limit (the right side of `/`) and the percentage are shown against the **limit in effect for that day**: a per-day override (set with the set-today-limit skill) if one exists for that date, otherwise the current base limit (env var / config / default).
- A day with no state files (no records) shows `$0.00`.
- To change the limit, use the set-limit skill; to delete the cumulative total, use the clear skill.
