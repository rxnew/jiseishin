# jiseishin

A Claude Code plugin that puts a self-limit on Claude Code's **daily cost**. When the (estimated) cumulative cost for the day reaches the limit, it blocks new prompts from being submitted. It is a personal guardrail aimed at **preventing billing accidents** in environments without usage rate limits (e.g. pay-as-you-go API key billing).

## Overview

- **Measurement unit:** Daily (the day's cumulative total)
- **Basis:** Cost (estimated USD). The `input_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` / `output_tokens` in each turn's `usage` are **converted to USD at the standard per-model API rate and summed**. Rather than adding tokens uniformly, it reflects the per-type rate differences (output is roughly 5x input, cache reads roughly 0.1x, etc.) and per-model rates.
- **Target rates:** **Specialized for Anthropic's standard API rates (pay-as-you-go).** Batch (half price), priority tier, and subscription usage limits are out of scope.
- **Behavior:** Once the limit is reached, new prompts are blocked at submission time, and an in-progress turn is also stopped mid-flight — at the next tool-batch boundary, before the next model call — so a runaway agent loop is caught within the turn rather than only at the next prompt.
- **Dependencies:** Python 3 standard library only. No external packages required.

> [!WARNING]
> Figures may differ significantly from your actual bill (see [Caveats](#caveats)). Treat them as a circuit breaker, not an accounting record or a substitute for the [Anthropic Console](https://console.anthropic.com/). Provided "as is" with no warranty (see [LICENSE](LICENSE)); the authors are not liable for any charges or losses.

> [!NOTE]
> This is an **estimate** based on Anthropic's standard API rates, not the actual billed amount. If you use Claude Code on a subscription (Pro / Max / Team / Enterprise), there is no per-prompt billing, and the amount shown by this plugin is only a guide for "what it would cost at standard API rates."

## Installation

Register the GitHub repository as a marketplace and install it:

```
/plugin marketplace add rxnew/jiseishin
/plugin install jiseishin@jiseishin
```

## Configuration

The limit is resolved in the order **env var > config file > default**. The default when unset is **$100/day**. This is a deliberately high circuit-breaker value to catch runaways in personal use; it is recommended to measure your actual usage for a few days with `/jiseishin:status`, then adjust it with `/jiseishin:set-limit` to fit your own numbers (e.g. about 1.2x your measured value).

### Setting and checking the limit (skills / natural language)

Use the bundled skills to set and check via conversation.

```
/jiseishin:set-limit 50      # set the limit to $50/day
/jiseishin:status            # check today's cost and limit
/jiseishin:status yesterday  # check the cost and limit for a given day
/jiseishin:clear             # delete today's state files (cumulative cost) to reset
/jiseishin:clear --all       # delete state files for all days (disk cleanup)
```

In addition to slash commands, natural language also triggers them (the `set-limit` skill interprets the amount):

- "Lower jiseishin's limit to 30 dollars" → `set-limit` skill
- "Check how much I spent today" / "How much did I spend yesterday?" → `status` skill (relative dates are converted to absolute dates before being passed)
- "Reset jiseishin's usage" → `clear` skill

The limit is saved in the config file `~/.config/jiseishin/config.json` (`{ "max_daily_cost_usd": <N> }`), and **changes take effect from the next prompt** (no restart of Claude Code needed).

Note that the only three things not blocked while the limit is reached are `/jiseishin:set-limit`, `/jiseishin:status`, and `/jiseishin:clear`; every other slash command and other plugins' skills are also subject to the limit. Because these are exempted, **even after hitting the limit you can resume by raising it with `/jiseishin:set-limit` or resetting today's total with `/jiseishin:clear`**.

### Temporary override (env var)

If you want to change the limit only for that shell / session, use the env var (it takes priority over the config file; applying it requires starting Claude Code).

```bash
export JISEISHIN_MAX_DAILY_COST_USD=200
```

## How the cost is calculated

Each assistant message's `usage` is converted at the standard API rate for its `model` and summed.

```
cost = Σ (per assistant message)
         input_tokens          × input rate
       + output_tokens         × output rate
       + cache_read_tokens     × input rate × 0.1
       + cache_write_5m_tokens × input rate × 1.25
       + cache_write_1h_tokens × input rate × 2.0
```

Because cache creation has different rates by TTL (5 minutes / 1 hour), the `cache_creation` breakdown (`ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`) is used; if the breakdown is absent, the aggregate value is billed at the 5-minute TTL rate.

### Per-model rates (standard API rates, USD per 1M tokens)

| Model family | Input | Output |
|------|-----:|-----:|
| Opus (4.5 / 4.6 / 4.7 / 4.8) | $5 | $25 |
| Sonnet (4.5 / 4.6) | $3 | $15 |
| Haiku 4.5 | $1 | $5 |
| Fable 5 / Mythos 5 | $10 | $50 |

- Versions within a family share the same rate, so matching is done by the model ID prefix (`claude-opus`, etc.). Context-length suffixes such as `[1m]`, date snapshots, and Bedrock/Vertex provider prefixes are normalized away.
- Cache rates are derived as multipliers of the input rate (read 0.1x, creation 5-minute 1.25x / 1-hour 2.0x).
- **Unknown models** (e.g. new models not in the price table) are billed at the top-tier Opus rate to avoid underestimating cost (= failing to block).
- Rates are hardcoded in `MODEL_PRICES` in [scripts/jiseishin.py](scripts/jiseishin.py). Update them here when prices change or a new model is added (reference: [Anthropic Models overview](https://platform.claude.com/docs/en/about-claude/models/overview)).

## How it works

It consists of [3 hooks](hooks/hooks.json) and [1 script](scripts/jiseishin.py).

| hook | mode | role |
|------|--------|------|
| `Stop` (at each turn end) | `record` | Folds the transcript lines appended since the last update into the main session's per-message records |
| `UserPromptSubmit` (at submission) | `check` | Sums today's cost across all contexts and, if at or above the limit, blocks the prompt with exit code 2 |
| `PostToolBatch` (after each tool batch, before the next model call) | `guard` | Folds in the lines appended since the last call (reading only the appended bytes) and, if today's total is at or above the limit, stops the agentic loop with exit code 2 |

State lives under `~/.local/state/jiseishin/`, keyed by `<key>` (the `session_id` for the main thread, `agent-<agent_id>` for a subagent):

- `days/<YYYY-MM-DD>/<key>.json` — a map of message id → cost (USD) for the responses that context billed on that day.
- `cursors/<key>.json` — `{path, offset, prompt}`: how far that context's transcript has been read, plus the last human prompt (for the mid-turn exemption check).

Subagents share the parent's `session_id` but each carries a distinct `agent_id`, and several can run in parallel (so `PostToolBatch` can fire concurrently); keying by `agent_id` gives every concurrent writer its own files, so no file is ever written by more than one process at a time and parallel writes cannot corrupt the state.

The day's total reads **only that day's files** (`days/<that-day>/*`), **merging the maps and deduplicating by message id**, then summing — so it stays fast no matter how much history has accumulated. The dedup matters because when a session is resumed or forked, Claude Code writes a new transcript (new `session_id`) that copies the prior conversation verbatim — same message ids and timestamps, only `sessionId` rewritten. Summing each context's cost independently would count that shared history once per context (inflating the total several-fold); deduplicating by message id removes it, and attributing each response to the local date of its own timestamp keeps a resumed-from-an-earlier-day session from booking old cost on today. Because a copied message keeps its original timestamp, duplicates always fall on the same day, so per-day dedup suffices. This mirrors how tools like `ccusage` compute usage.

### Caveats

- The amount is an **estimate** based on standard API rates and does not match the actual billed amount (differences arise from price-table freshness, the unknown-model fallback, rounding, etc.).
- The mid-turn `guard` (`PostToolBatch`) fires after each tool batch, before the next model call, so an over-limit turn is stopped within itself, not only at the next submission. The residual overage is therefore at most **one model call's worth** — the call that pushed the total over the limit has already been billed by the time the hook sees it. With multiple sessions or parallel subagents running, the residual is bounded by "one model call's worth per running context."
- A turn that makes **no tool calls** (a single large text response), or a single in-flight streaming response, cannot be interrupted partway; those cases are still caught at the next submission.
- Subagent costs: when `PostToolBatch` fires inside a subagent, that subagent's own token cost is counted under `agent-<agent_id>` (more accurate than counting only the main transcript). A subagent's final message after its last tool batch is not aggregated (there is no `SubagentStop` recording), so it can be slightly undercounted.
- Date boundaries follow the system's local date. Each response is attributed to the local date of its own transcript timestamp, so a session that spans midnight (or is resumed from an earlier day) has its cost split correctly across days rather than all booked on the update day.
- If the guard itself errors, it **fails open** (lets the prompt through). This prevents a guard bug from making Claude completely unusable.

## Development

### Running the tests

The tests use only the Python standard library (`unittest`), so no setup or dependencies are needed. Run them from the repository root:

```bash
python3 -m unittest discover -s tests
```

Run a single test module or case with verbose output:

```bash
python3 -m unittest -v tests.test_jiseishin
python3 -m unittest -v tests.test_jiseishin.CrossContextDedupTest
```

The tests point `STATE_ROOT` at a temporary directory and pin `TZ=UTC`, so they never touch your real state and are independent of the machine's timezone.
