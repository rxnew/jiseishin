#!/usr/bin/env python3
"""jiseishin — daily cost self-limit for Claude Code.

What is measured is the "cost": the input, cache-creation, cache-read, and
output tokens in a turn's usage, each converted to USD at the standard API
rate for its model. Tokens are not summed uniformly; the per-type rate
differences (output is roughly 5x input, cache reads roughly 0.1x, etc.) and
the per-model rates are reflected.

Scope is limited to Anthropic's standard API rates (pay-as-you-go). Batch
(half price), priority tier, and subscription usage limits are out of scope.

There are 6 modes. The first 3 are called from hooks, the last 3 by hand:

  record    : Called from the Stop hook. Aggregates the current session's
              transcript and records "the session's cumulative cost (USD)"
              into today's state file.
  check     : Called from the UserPromptSubmit hook. Blocks prompt submission
              with exit code 2 if today's cumulative total across all sessions
              is at or above the limit.
  guard     : Called from the PostToolBatch hook (after each tool batch, before
              the next model call). Recomputes the live cumulative total
              mid-turn and stops the agentic loop with exit code 2 if it is at
              or above the limit, so a runaway loop is caught within the turn
              instead of only at the next prompt.
  set-limit : Saves the daily cost limit (USD) to the config file.
              Example: jiseishin.py set-limit 50
  status    : Shows the cost for a given day (today by default) and the
              current limit. Example: jiseishin.py status 2026-06-18
  clear     : Deletes state files to reset the cumulative total. By default
              only today's, or all days with --all.
              Example: jiseishin.py clear / jiseishin.py clear --all

State is stored as one cumulative-cost (USD) value per execution context at
<state_root>/<YYYY-MM-DD>/<key>, where <key> is the session_id for the main
thread and "agent-<agent_id>" for a subagent. Subagents share the parent's
session_id but each carries a distinct agent_id and several can run in parallel,
so keying by agent_id gives every concurrent writer its own file: no file is
ever written by more than one process at a time, so parallel writes cannot
corrupt state. The `guard` mode additionally keeps a per-context
incremental-read cache (byte offset + partial sum) under
<state_root>/<YYYY-MM-DD>/.cache/<key>, so each call scans only the bytes
appended to the transcript since the previous call.

The limit is resolved in the order "env var JISEISHIN_MAX_DAILY_COST_USD >
config file > default". The config file can be updated with `set-limit`, and
changes take effect from the next prompt (unlike the env var, no restart of
Claude Code is needed).
To avoid the guard itself making Claude unusable, any unexpected error in the
hooks (record/check) is swallowed and falls back to allowing the prompt
(fail-open).

Rates are the standard API rates (USD per 1M tokens). When prices change or a
new model is added, update MODEL_PRICES.
Reference: https://platform.claude.com/docs/en/about-claude/models/overview
"""
import os
import re
import sys
import json
import glob
import shutil
import datetime

DEFAULT_LIMIT_USD = 100.0

STATE_ROOT = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "jiseishin",
)
CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "jiseishin",
    "config.json",
)

# Subdirectory (under each day's state dir) holding guard's incremental-read
# caches. The dotted name keeps it out of date_cost()'s "*" glob.
CACHE_DIRNAME = ".cache"

# Standard API rates (USD per 1M tokens). (input, output) per model family.
# Versions within a family (Opus 4.5/4.6/4.7/4.8, etc.) share the same rate,
# so matching is done by prefix.
# Update this when prices change or a new family is added.
# Reference: https://platform.claude.com/docs/en/about-claude/models/overview
MODEL_PRICES = (
    ("claude-opus", (5.0, 25.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku", (1.0, 5.0)),
    ("claude-fable", (10.0, 50.0)),
    ("claude-mythos", (10.0, 50.0)),
)
# Unknown models are billed at the top-tier Opus rate to avoid underestimating
# cost (= failing to block).
DEFAULT_PRICE = (5.0, 25.0)

# Cache rate multipliers relative to the input rate (standard API rate).
CACHE_READ_MULTIPLIER = 0.1       # cache read
CACHE_WRITE_5M_MULTIPLIER = 1.25  # cache creation (5-minute TTL)
CACHE_WRITE_1H_MULTIPLIER = 2.0   # cache creation (1-hour TTL)


def state_dir(date):
    """Return the state directory for a given day (<state_root>/<YYYY-MM-DD>)."""
    return os.path.join(STATE_ROOT, date.isoformat())


def today_dir():
    return state_dir(datetime.date.today())


def read_config():
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def limit():
    """Resolve the limit (USD) in the order "env var > config file > default"."""
    raw = os.environ.get("JISEISHIN_MAX_DAILY_COST_USD")
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
    configured = read_config().get("max_daily_cost_usd")
    if isinstance(configured, (int, float)) and not isinstance(configured, bool) and configured >= 0:
        return float(configured)
    return DEFAULT_LIMIT_USD


def read_stdin():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def normalize_model(model):
    """Normalize a transcript model string for matching against the price table.
    Removes provider/region prefixes (anthropic. / us. etc.), context-length
    suffixes ([1m] etc.), and date snapshots (-YYYYMMDD / @YYYYMMDD)."""
    if not isinstance(model, str):
        return ""
    name = model.strip().lower()
    name = re.sub(r"^(us|eu|apac|global)\.", "", name)
    name = re.sub(r"^anthropic\.", "", name)
    name = re.sub(r"\[[^\]]*\]", "", name)
    name = re.sub(r"[-@]\d{8}$", "", name)
    return name


def price_for_model(model):
    """Return (input_rate, output_rate) in USD per 1M tokens. Unknown models map to Opus."""
    name = normalize_model(model)
    for prefix, price in MODEL_PRICES:
        if name.startswith(prefix):
            return price
    return DEFAULT_PRICE


def usage_cost_usd(usage, model):
    """Convert a single assistant message's usage to USD at the standard API rate.

    input_tokens is non-cached input and does not overlap with cache_read or
    cache_creation, so it can simply be added. Cache creation has different
    rates by TTL (5 minutes / 1 hour), so the cache_creation breakdown is used;
    if absent, the aggregate value is billed at the 5-minute TTL rate."""
    input_rate, output_rate = price_for_model(model)

    def tokens(field):
        value = usage.get(field)
        return value if isinstance(value, int) else 0

    # Accumulate tokens × (USD per 1M tokens), then divide by 1M at the end.
    weighted = 0.0
    weighted += tokens("input_tokens") * input_rate
    weighted += tokens("output_tokens") * output_rate
    weighted += tokens("cache_read_input_tokens") * input_rate * CACHE_READ_MULTIPLIER

    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict) and (
        "ephemeral_5m_input_tokens" in cache_creation
        or "ephemeral_1h_input_tokens" in cache_creation
    ):
        w5 = cache_creation.get("ephemeral_5m_input_tokens")
        w1 = cache_creation.get("ephemeral_1h_input_tokens")
        weighted += (w5 if isinstance(w5, int) else 0) * input_rate * CACHE_WRITE_5M_MULTIPLIER
        weighted += (w1 if isinstance(w1, int) else 0) * input_rate * CACHE_WRITE_1H_MULTIPLIER
    else:
        weighted += tokens("cache_creation_input_tokens") * input_rate * CACHE_WRITE_5M_MULTIPLIER

    return weighted / 1_000_000


def sum_session_cost(transcript_path):
    """Scan a transcript JSONL and sum the usage of assistant messages,
    converting to USD at the per-model standard API rate."""
    total = 0.0
    with open(transcript_path, "r", errors="replace") as fh:
        for line in fh:
            # Lightweight filter targeting only lines with usage (assistant
            # messages). usage always includes output_tokens, so we test for it.
            if "output_tokens" not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            message = obj.get("message") or {}
            usage = message.get("usage") or {}
            if usage:
                total += usage_cost_usd(usage, message.get("model"))
    return total


def context_key(data):
    """Unique key for the current execution context.

    Subagents share the parent's session_id but each carries a distinct
    agent_id, and several subagents can run in parallel. Keying by agent_id when
    present (else session_id) gives every concurrent writer its own state and
    cache file, so no file is ever written by more than one process at a time."""
    agent_id = data.get("agent_id")
    if agent_id:
        return "agent-" + str(agent_id)
    return data.get("session_id")


def human_prompt_text(content):
    """Return the text of a human prompt from a transcript user message, or None
    if it is a tool-result turn (a list of tool_result blocks) rather than a
    prompt the user actually typed."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            return None
        text = "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return text or None
    return None


def cache_path(key):
    return os.path.join(today_dir(), CACHE_DIRNAME, key)


def incremental_session_cost(key, transcript_path):
    """Return (cumulative_cost_usd, last_human_prompt) for a transcript, reading
    only the bytes appended since the previous call.

    The running byte offset, partial sum, and last human prompt are cached per
    context under <today>/.cache/<key>. Only this context writes that file, so
    parallel subagents (distinct agent_id) never collide on it."""
    try:
        with open(cache_path(key)) as fh:
            cache = json.load(fh)
        if not isinstance(cache, dict):
            cache = {}
    except Exception:
        cache = {}

    if cache.get("path") == transcript_path:
        offset = cache.get("offset", 0)
        total = cache.get("sum", 0.0)
        prompt = cache.get("prompt", "")
    else:
        offset, total, prompt = 0, 0.0, ""
    # Reset on a missing/rotated/truncated transcript (offset past EOF) so we
    # never skip lines and undercount.
    if not isinstance(offset, int) or offset < 0 or offset > os.path.getsize(transcript_path):
        offset, total, prompt = 0, 0.0, ""

    with open(transcript_path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    # Consume only whole newline-terminated lines; a trailing partial line (one
    # still being written) is left for the next call.
    consumed = chunk.rfind(b"\n") + 1
    for raw in chunk[:consumed].splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        message = obj.get("message") or {}
        usage = message.get("usage") or {}
        if usage:
            total += usage_cost_usd(usage, message.get("model"))
        elif obj.get("type") == "user":
            text = human_prompt_text(message.get("content"))
            if text is not None:
                prompt = text

    record = {"path": transcript_path, "offset": offset + consumed, "sum": total, "prompt": prompt}
    os.makedirs(os.path.dirname(cache_path(key)), exist_ok=True)
    atomic_write(cache_path(key), json.dumps(record))
    return total, prompt


def date_cost(date):
    """Sum the cumulative cost (USD) across all sessions for a given day."""
    directory = state_dir(date)
    total = 0.0
    if os.path.isdir(directory):
        for path in glob.glob(os.path.join(directory, "*")):
            if path.endswith(".tmp"):
                continue
            try:
                with open(path) as fh:
                    total += float(fh.read().strip() or 0)
            except Exception:
                continue
    return total


def today_cost():
    """Sum the cumulative cost (USD) across all of today's sessions."""
    return date_cost(datetime.date.today())


def atomic_write(path, text):
    """Write to a temp file then rename, so readers never see partial content.
    The temp name includes the pid so two processes writing different files never
    share a temp path; os.replace is atomic, so a file is never left corrupted."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def fmt_usd(amount):
    return f"${amount:,.2f}"


def cmd_record(data):
    session_id = data.get("session_id")
    transcript_path = data.get("transcript_path")
    if not session_id or not transcript_path or not os.path.exists(transcript_path):
        return 0
    total = sum_session_cost(transcript_path)
    directory = today_dir()
    os.makedirs(directory, exist_ok=True)
    atomic_write(os.path.join(directory, session_id), repr(total))
    return 0


# Slash commands that are never blocked even when the limit is reached. Only the
# means to raise the limit / reset the total / check status are left open
# (blocking them would make it impossible to recover from hitting the limit).
# Every other slash command and free-form prompt is subject to the limit.
EXEMPT_COMMANDS = ("/jiseishin:set-limit", "/jiseishin:status", "/jiseishin:clear")


def cmd_check(data):
    prompt = (data.get("prompt") or "").lstrip()
    if any(prompt == cmd or prompt.startswith(cmd + " ") for cmd in EXEMPT_COMMANDS):
        return 0
    total = today_cost()
    threshold = limit()
    if total >= threshold:
        sys.stderr.write(
            f"[jiseishin] Daily cost limit reached: {fmt_usd(total)} / {fmt_usd(threshold)}.\n"
            "New prompt blocked. To continue, raise the limit with "
            "/jiseishin:set-limit <USD>, or wait until the date changes.\n"
        )
        return 2
    return 0


def cmd_guard(data):
    transcript_path = data.get("transcript_path")
    key = context_key(data)
    if not key or not transcript_path or not os.path.exists(transcript_path):
        return 0
    cost, prompt = incremental_session_cost(key, transcript_path)
    # Publish this context's live cost so parallel contexts and the check below
    # see it (record only refreshes the main session's file at turn end).
    directory = today_dir()
    os.makedirs(directory, exist_ok=True)
    atomic_write(os.path.join(directory, key), repr(cost))
    # Honor the same exemptions as the submit-time check, so a turn started by a
    # recovery/inspection command can finish: read-only status, and raising the
    # limit or resetting the total, would otherwise be cut off mid-turn.
    prompt = (prompt or "").lstrip()
    if any(prompt == cmd or prompt.startswith(cmd + " ") for cmd in EXEMPT_COMMANDS):
        return 0
    total = today_cost()
    threshold = limit()
    if total >= threshold:
        sys.stderr.write(
            f"[jiseishin] Daily cost limit reached mid-turn: {fmt_usd(total)} / {fmt_usd(threshold)}.\n"
            "Stopping the agentic loop before the next model call. To continue, raise the "
            "limit with /jiseishin:set-limit <USD>, reset with /jiseishin:clear, or wait "
            "until the date changes.\n"
        )
        return 2
    return 0


def cmd_set_limit(args):
    if not args:
        sys.stderr.write("usage: jiseishin.py set-limit <usd>\n")
        return 1
    try:
        value = float(args[0])
    except ValueError:
        sys.stderr.write(f"[jiseishin] Please specify a number (USD): {args[0]}\n")
        return 1
    if value < 0:
        sys.stderr.write("[jiseishin] The limit must be 0 or greater.\n")
        return 1
    if value.is_integer():
        value = int(value)
    config = read_config()
    config["max_daily_cost_usd"] = value
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    atomic_write(CONFIG_PATH, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    print(f"[jiseishin] Saved max_daily_cost_usd = {value} ({fmt_usd(value)}/day) ({CONFIG_PATH})")
    return 0


def cmd_status(args):
    if args:
        try:
            date = datetime.date.fromisoformat(args[0])
        except ValueError:
            sys.stderr.write(f"[jiseishin] Please specify the date in YYYY-MM-DD format: {args[0]}\n")
            return 1
    else:
        date = datetime.date.today()
    total = date_cost(date)
    threshold = limit()
    pct = (total / threshold * 100) if threshold else 0.0
    print(f"date          : {date.isoformat()}")
    print(f"total cost    : {fmt_usd(total)} / {fmt_usd(threshold)} ({pct:.1f}%)")
    env_override = os.environ.get("JISEISHIN_MAX_DAILY_COST_USD")
    configured = read_config().get("max_daily_cost_usd")
    if env_override:
        print(f"source        : env var JISEISHIN_MAX_DAILY_COST_USD={env_override}")
    elif isinstance(configured, (int, float)) and not isinstance(configured, bool):
        print(f"source        : config file {CONFIG_PATH}")
    else:
        print(f"source        : default {fmt_usd(DEFAULT_LIMIT_USD)}")
    print("rates         : Anthropic standard API rates (USD/MTok; batch/priority/subscription not supported)")
    return 0


def cmd_clear(args):
    """Delete state files to reset the cumulative total. By default only today's,
    or all days with --all.

    Deleting today's state resets today's cumulative total to 0, and if a prompt
    was blocked by hitting the limit, the next prompt will go through. However,
    in-progress sessions are re-aggregated from the full transcript and written
    back on the next Stop hook, so their costs reappear (costs from already-ended
    sessions do not).
    """
    extra = [arg for arg in args if arg != "--all"]
    if extra:
        sys.stderr.write("usage: jiseishin.py clear [--all]\n")
        return 1

    if "--all" in args:
        target = STATE_ROOT
        scope = "all days"
    else:
        target = today_dir()
        scope = f"today ({datetime.date.today().isoformat()})"

    if not os.path.isdir(target):
        print(f"[jiseishin] No state files to delete ({scope}: {target})")
        return 0

    file_count = sum(len(files) for _root, _dirs, files in os.walk(target))
    shutil.rmtree(target)
    print(f"[jiseishin] Deleted state files for {scope} ({file_count} file(s), {target})")
    return 0


def main():
    argv = sys.argv[1:]
    mode = argv[0] if argv else ""
    rest = argv[1:]

    # Manual commands do not read stdin (so running them in an interactive
    # terminal does not block).
    if mode == "set-limit":
        return cmd_set_limit(rest)
    if mode == "status":
        return cmd_status(rest)
    if mode == "clear":
        return cmd_clear(rest)

    # Hook modes receive the hook input from stdin.
    data = read_stdin()
    try:
        if mode == "record":
            return cmd_record(data)
        if mode == "check":
            return cmd_check(data)
        if mode == "guard":
            return cmd_guard(data)
    except Exception as error:
        sys.stderr.write(f"[jiseishin] hook error (ignored): {error}\n")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
