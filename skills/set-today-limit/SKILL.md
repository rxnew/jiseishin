---
name: set-today-limit
description: Raise (or lower) jiseishin's cost limit for today only, leaving the base limit unchanged. Use this when the user wants a one-day bump like "raise today's limit", "just for today", "bump the cap for today", or to keep going today after hitting the limit but revert to the normal limit tomorrow.
argument-hint: "[usd]"
---

# Setting jiseishin's limit for today only

Set a **per-day limit override** for today. It applies to today only and self-expires: from tomorrow, the limit falls back to the base value set with set-limit (or the env var / default). Use this when the user wants to keep working today past the usual cap without permanently changing it.

## Steps

1. Determine the user's desired limit for today as a USD amount.
   - Convert notations like "50 dollars", "$50", or "up to 30 today" into a number (e.g. `$50` → `50`; decimals allowed: `12.5`).
   - If stated in yen, explain that USD is the basis and confirm the desired USD amount (no currency conversion is performed).
   - If the amount is ambiguous or unspecified, first check the current spend and limit (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" status`), then ask the user for the desired value.
2. Run the following command (replace `<USD>` with the number):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" set-today-limit <USD>
   ```

3. Briefly report the result (today's date and the configured value) to the user, and note that it reverts to the base limit tomorrow.

If the user wants to change the limit **permanently** (every day), use the set-limit skill instead. If they only ask to check usage, use the status skill.

## Notes

- The override is saved in the config file `~/.config/jiseishin/config.json` (key `daily_limits`, a map of `YYYY-MM-DD` → USD) and **takes effect from the next prompt** (no restart of Claude Code needed).
- It applies to **today only**. Other days are unaffected, and past-day entries are pruned automatically when a new one is set, so the config does not grow without bound.
- For today, the per-day override wins over the base `max_daily_cost_usd`, but the env var `JISEISHIN_MAX_DAILY_COST_USD` (a hard external ceiling) still takes priority over it. Resolution order: **env var > per-day override > config file > default**.
- It is fine to use this to **lower** today's limit too (any value ≥ 0); the mechanism is general, not raise-only.
- If a prompt was blocked by reaching the limit, raising today's limit lets the next prompt through (`/jiseishin:set-today-limit` itself is not blocked even while the limit is reached). Alternatively, reset the total with the clear skill.
- Rates are specialized for the standard API rates (pay-as-you-go). Batch, priority tier, and subscription usage limits are out of scope.
