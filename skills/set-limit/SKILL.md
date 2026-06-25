---
name: set-limit
description: Set or change jiseishin's daily cost limit (USD). Use this when the user wants to set, change, raise, or lower the "cost limit", "cost cap", "limit", or "daily budget", or to check the current spend or limit.
argument-hint: "[usd]"
---

# Setting jiseishin's daily cost limit

Set or change the jiseishin plugin's "daily cost limit (USD)". The cost is an estimate of the day's total tokens converted to USD at Anthropic's standard API rates; once the limit is reached, new prompts are blocked for the rest of that day.

## Steps

1. Determine the user's desired limit as a USD amount.
   - Convert notations like "50 dollars", "$50", or "up to 30 a day" into a number (e.g. `$50` → `50`; decimals allowed: `12.5`).
   - If stated in yen, explain that USD is the basis and confirm the desired USD amount (no currency conversion is performed).
   - If the limit is ambiguous or unspecified, first check the current value (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" status`), then ask the user for the desired value.
2. Run the following command (replace `<USD>` with the number):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" set-limit <USD>
   ```

3. Briefly report the result (the save path and the configured value) to the user.

If the user only asks to check usage, do not change the limit; use the status skill instead. If they want to raise the limit **for today only** (reverting to the normal limit tomorrow), use the set-today-limit skill instead.

## Notes

- The limit is saved in the config file `~/.config/jiseishin/config.json` (key `max_daily_cost_usd`) and **takes effect from the next prompt** (no restart of Claude Code needed).
- If the env var `JISEISHIN_MAX_DAILY_COST_USD` is set, it takes priority over everything. Below it, a per-day override set with set-today-limit takes priority over this base limit, but only for its own day. Resolution order: env var > per-day override > config file > default.
- The default is $100/day (a deliberately high circuit-breaker value to catch runaways in personal use; it is recommended to measure your usage for a few days with status and adjust to fit your numbers).
- Rates are specialized for the standard API rates (pay-as-you-go). Batch, priority tier, and subscription usage limits are out of scope.
