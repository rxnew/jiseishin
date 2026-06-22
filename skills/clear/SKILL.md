---
name: clear
description: Delete jiseishin's state files (the daily cumulative cost) to reset the cumulative total. Use this when the user wants to "reset usage", "reset the cost", "clear the counter", "delete the state files", or "wipe the cumulative total", or to lift a blocked state caused by reaching the limit. Does not change the configuration (limit).
argument-hint: "[--all]"
---

# Resetting jiseishin's cumulative total

Reset the jiseishin plugin's cumulative cost (stored under `~/.local/state/jiseishin/days/<date>/<key>.json`) back to 0. The limit (config file) is not changed.

## Steps

1. Determine the scope.
   - The default is **today only** (only today's total is used for the limit check, so this is enough to reset / lift the block). This drops today's recorded cost while keeping other days' totals.
   - If the user says "everything", "the past too", "all days", "clean up old state files", etc., use `--all` (delete state files for all days = disk cleanup).
2. Because the reset cannot be undone, confirm the scope (today only / all days) with the user before running.
3. Run the following command:

   ```bash
   # delete today only (default)
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" clear

   # delete all days
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" clear --all
   ```

4. Briefly report the result (number of records dropped / files deleted) to the user.

If the user only asks to check usage, do not reset; use the status skill instead. If they want to change the limit, use the set-limit skill.

## Notes

- **Resetting today sets today's cumulative total to 0**, and if a prompt was blocked by reaching the limit, the next prompt will go through (`/jiseishin:clear` itself is not blocked even while the limit is reached).
- **In-progress sessions then count only new usage from this point on** — the already-counted messages are not re-added (the read offset is kept, so they stay dropped). To raise the limit permanently, use set-limit instead.
- It never touches the config file or env var for the limit (`max_daily_cost_usd`).
