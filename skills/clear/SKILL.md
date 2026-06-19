---
name: clear
description: Delete jiseishin's state files (the daily cumulative cost) to reset the cumulative total. Use this when the user wants to "reset usage", "reset the cost", "clear the counter", "delete the state files", or "wipe the cumulative total", or to lift a blocked state caused by reaching the limit. Does not change the configuration (limit).
argument-hint: "[--all]"
---

# Deleting jiseishin state files (resetting the cumulative total)

Delete the jiseishin plugin's state files (the daily cumulative cost stored at `~/.local/state/jiseishin/<date>/<session_id>`) and reset the cumulative total to 0. The limit (config file) is not changed.

## Steps

1. Determine the deletion scope.
   - The default is **today only** (only today's total is used for the limit check, so this is enough to reset / lift the block).
   - If the user says "everything", "the past too", "all days", "clean up old state files", etc., use `--all` (delete state files for all days = disk cleanup).
2. Because deletion cannot be undone, confirm the scope (today only / all days) with the user before running.
3. Run the following command:

   ```bash
   # delete today only (default)
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" clear

   # delete all days
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/jiseishin.py" clear --all
   ```

4. Briefly report the result (number of files deleted and the deletion path) to the user.

If the user only asks to check usage, do not delete; use the status skill instead. If they want to change the limit, use the set-limit skill.

## Notes

- **Deleting today's state resets today's cumulative total to 0**, and if a prompt was blocked by reaching the limit, the next prompt will go through (`/jiseishin:clear` itself is not blocked even while the limit is reached).
- However, **in-progress sessions are re-aggregated from the full transcript and written back on the next turn end (Stop hook)**, so that session's cost reappears. Costs from already-ended past sessions do not reappear. To raise the limit permanently, use set-limit.
- It never touches the config file or env var for the limit (`max_daily_cost_usd`). Only the state files are deleted.
