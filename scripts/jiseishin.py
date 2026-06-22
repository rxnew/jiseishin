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

  record    : Called from the Stop hook. Folds any transcript lines appended
              since the last update into the main session's per-message records.
  check     : Called from the UserPromptSubmit hook. Blocks prompt submission
              with exit code 2 if today's cumulative total across all contexts
              is at or above the limit.
  guard     : Called from the PostToolBatch hook (after each tool batch, before
              the next model call). Folds in the lines appended since the last
              call and stops the agentic loop with exit code 2 if today's total
              is at or above the limit, so a runaway loop is caught within the
              turn instead of only at the next prompt.
  set-limit : Saves the daily cost limit (USD) to the config file.
              Example: jiseishin.py set-limit 50
  status    : Shows the cost for a given day (today by default) and the
              current limit. Example: jiseishin.py status 2026-06-18
  clear     : Resets the cumulative total. By default only today's (other days
              are kept), or every day with --all.
              Example: jiseishin.py clear / jiseishin.py clear --all

State has two parts, both keyed by <key> = the session_id for the main thread
or "agent-<agent_id>" for a subagent (so each concurrent writer owns its files
and parallel writes never collide):

  days/<YYYY-MM-DD>/<key>.json  : map of message id -> cost (USD) for the
                                  responses that context billed on that day.
  cursors/<key>.json            : {path, offset, prompt} — how far the
                                  transcript has been read, plus the last human
                                  prompt (used for the mid-turn exemption check).

The day's total (date_cost) reads ONLY days/<that-day>/*, merging the maps and
deduplicating by message id, then summing. Reading just one day keeps the cost
bounded by that day's activity no matter how much history has accumulated.

Two things this design handles, which a naive "sum each session's cost" would
get wrong: when a session is resumed/forked, Claude Code writes a NEW transcript
(new session_id) that copies the prior conversation verbatim (same message ids
and timestamps, only sessionId is rewritten); merging by message id collapses
those copies to one. And attributing each response to the local date of its own
timestamp keeps a session that spans midnight (or is resumed from an earlier
day) from booking old cost on today. Because a copied message keeps its original
timestamp, duplicates always fall on the same day, so per-day dedup suffices.

The limit is resolved in the order "env var JISEISHIN_MAX_DAILY_COST_USD >
config file > default". The config file can be updated with `set-limit`, and
changes take effect from the next prompt (unlike the env var, no restart of
Claude Code is needed).
To avoid the guard itself making Claude unusable, any unexpected error in the
hooks (record/check/guard) is swallowed and falls back to allowing the prompt
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

# Subdirectories under STATE_ROOT. Per-day message costs live in
# days/<YYYY-MM-DD>/<key>.json (so a day's total reads only that day's files,
# regardless of how much history has accumulated); per-context read cursors live
# in cursors/<key>.json.
DAYS_DIRNAME = "days"
CURSORS_DIRNAME = "cursors"

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


def day_dir(date_str):
    """Directory holding per-context cost maps for one day."""
    return os.path.join(STATE_ROOT, DAYS_DIRNAME, date_str)


def day_file(date_str, key):
    return os.path.join(day_dir(date_str), key + ".json")


def cursors_dir():
    return os.path.join(STATE_ROOT, CURSORS_DIRNAME)


def cursor_path(key):
    return os.path.join(cursors_dir(), key + ".json")


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


def message_date(obj):
    """Return the local-date (YYYY-MM-DD) a transcript line should be billed on.

    Transcript timestamps are ISO-8601 UTC ("...Z"); we convert to the system's
    local timezone so day boundaries match `datetime.date.today()` used
    elsewhere. A missing/unparseable timestamp falls back to today so the cost
    is still counted rather than silently dropped."""
    ts = obj.get("timestamp")
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone()  # -> local timezone
            return dt.date().isoformat()
        except Exception:
            pass
    return datetime.date.today().isoformat()


def dedup_key(obj, message):
    """A key identifying one billed response, stable across the content-block
    lines that repeat it and across the transcript copies a resume/fork makes.

    message.id is the response id and is repeated on every content-block line of
    that response; requestId is its sibling. uuid is per-line (last resort, only
    reached when a usage line carries neither id nor requestId, which does not
    happen for normal assistant responses)."""
    return message.get("id") or obj.get("requestId") or obj.get("uuid")


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


def context_key(data):
    """Unique key for the current execution context.

    Subagents share the parent's session_id but each carries a distinct
    agent_id, and several subagents can run in parallel. Keying by agent_id when
    present (else session_id) gives every concurrent writer its own state file,
    so no file is ever written by more than one process at a time."""
    agent_id = data.get("agent_id")
    if agent_id:
        return "agent-" + str(agent_id)
    return data.get("session_id")


def resolve_transcript(data):
    """Return the transcript whose cost belongs to THIS context.

    A hook firing inside a subagent is handed the PARENT session's
    transcript_path, not the subagent's own. Reading that parent under each
    distinct agent_id key would re-read the parent once per parallel subagent.
    Cross-context dedup by message id (see date_cost) would collapse those
    re-reads, but a subagent should still read its OWN transcript, which sits
    beside the parent at <parent-without-.jsonl>/subagents/<context_key>.jsonl.

    That layout is an observed Claude Code internal, not a documented hook
    contract, so it may change without notice; this is why we probe with
    os.path.exists() below and fall back to counting nothing (never the parent)
    rather than trusting the derived path blindly.

    The leaf is derived from context_key() so the state-file key (agent-<id>)
    and the transcript filename (agent-<id>.jsonl) can never drift apart."""
    path = data.get("transcript_path")
    if not path:
        return None
    if not data.get("agent_id"):
        return path  # main thread: the handed path is already its own
    leaf = context_key(data) + ".jsonl"
    if os.path.basename(path) == leaf:
        return path  # forward-compat: a future version may hand the own path
    base = path[:-len(".jsonl")] if path.endswith(".jsonl") else path
    own = os.path.join(base, "subagents", leaf)
    if os.path.exists(own):
        return own
    # Own transcript not locatable: count NOTHING rather than re-reading the
    # parent (that re-read would be collapsed by dedup, but reading it at all is
    # pointless and risks miscounting under future layout changes).
    return None


def load_json(path):
    """Load a JSON object from path, or {} if missing/corrupt/not an object."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def update_context(key, transcript_path):
    """Fold transcript lines appended since the last call into the per-day cost
    maps for this context, persist them and the read cursor, and return the last
    human prompt.

    Each new assistant response is written to days/<its-date>/<key>.json as
    message_id -> cost. Keying by message_id means the content-block lines that
    repeat one response's id and usage are counted once, and the read cursor
    (byte offset) means a line is processed only when its bytes are first read,
    so a line straddling a call boundary is never recounted. Only this context
    writes its own files, so parallel subagents (distinct agent_id) never
    collide. A response is attributed to the local date of its own timestamp, so
    its cost lands in the right day even for a session that spans midnight or is
    resumed from an earlier day."""
    cursor = load_json(cursor_path(key))
    same_path = cursor.get("path") == transcript_path
    offset = cursor.get("offset", 0) if same_path else 0
    prompt = cursor.get("prompt", "") if same_path else ""

    size = os.path.getsize(transcript_path)
    # Re-scan from the top on a path change or a rotated/truncated transcript
    # (offset past EOF) so we never skip lines and undercount. Drop this
    # context's prior day-file entries first so the re-scan rebuilds them
    # cleanly rather than leaving stale ids behind.
    if not same_path or not isinstance(offset, int) or offset < 0 or offset > size:
        offset, prompt = 0, ""
        for stale in glob.glob(os.path.join(STATE_ROOT, DAYS_DIRNAME, "*", key + ".json")):
            try:
                os.remove(stale)
            except OSError:
                pass

    with open(transcript_path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    # Consume only whole newline-terminated lines; a trailing partial line (one
    # still being written) is left for the next call.
    consumed = chunk.rfind(b"\n") + 1
    costs_by_date = {}
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
            mkey = dedup_key(obj, message)
            if mkey is not None:
                costs_by_date.setdefault(message_date(obj), {})[mkey] = \
                    usage_cost_usd(usage, message.get("model"))
        elif obj.get("type") == "user":
            text = human_prompt_text(message.get("content"))
            if text is not None:
                prompt = text

    for date_str, costs in costs_by_date.items():
        merged = load_json(day_file(date_str, key))
        merged.update(costs)
        os.makedirs(day_dir(date_str), exist_ok=True)
        atomic_write(day_file(date_str, key), json.dumps(merged))

    os.makedirs(cursors_dir(), exist_ok=True)
    atomic_write(cursor_path(key), json.dumps(
        {"path": transcript_path, "offset": offset + consumed, "prompt": prompt}))
    return prompt


def date_cost(date):
    """Sum the cost (USD) billed on a given day, deduped across all contexts.

    Reads only that day's files. The same message_id appears in several
    contexts' files when a session is resumed/forked (the new transcript copies
    prior messages, keeping their ids and timestamps); merging by id collapses
    those copies to one. Duplicates always share the original timestamp, hence
    the same day, so per-day dedup is sufficient."""
    merged = {}
    directory = day_dir(date.isoformat())
    if os.path.isdir(directory):
        for path in glob.glob(os.path.join(directory, "*.json")):
            for mkey, cost in load_json(path).items():
                if isinstance(cost, (int, float)):
                    merged[mkey] = cost
    return sum(merged.values())


def today_cost():
    """Sum the cost (USD) billed today, deduped across all contexts."""
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
    transcript_path = resolve_transcript(data)
    key = context_key(data)
    if not key or not transcript_path or not os.path.exists(transcript_path):
        return 0
    update_context(key, transcript_path)
    return 0


# Slash commands that are never blocked even when the limit is reached. Only the
# means to raise the limit / reset the total / check status are left open
# (blocking them would make it impossible to recover from hitting the limit).
# Every other slash command and free-form prompt is subject to the limit.
EXEMPT_COMMANDS = ("/jiseishin:set-limit", "/jiseishin:status", "/jiseishin:clear")


def is_exempt(prompt):
    prompt = (prompt or "").lstrip()
    return any(prompt == cmd or prompt.startswith(cmd + " ") for cmd in EXEMPT_COMMANDS)


def cmd_check(data):
    if is_exempt(data.get("prompt")):
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
    transcript_path = resolve_transcript(data)
    key = context_key(data)
    if not key or not transcript_path or not os.path.exists(transcript_path):
        if data.get("agent_id") and not transcript_path:
            # Surface layout drift rather than silently undercounting: we do NOT
            # fall back to the parent transcript (that re-read is the bug).
            sys.stderr.write(
                f"[jiseishin] subagent transcript not found ({key}); "
                "skipping its cost this batch.\n"
            )
        return 0
    prompt = update_context(key, transcript_path)
    # Honor the same exemptions as the submit-time check, so a turn started by a
    # recovery/inspection command can finish: read-only status, and raising the
    # limit or resetting the total, would otherwise be cut off mid-turn.
    if is_exempt(prompt):
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


def clear_today():
    """Drop today's cost maps, keeping other days and every context's read
    cursor.

    Keeping the cursors means an in-progress session does NOT re-count the
    messages just cleared (they sit behind the byte offset) and only new
    activity is added going forward — a clean "reset today, count fresh from
    here". Returns the number of message records removed."""
    directory = day_dir(datetime.date.today().isoformat())
    if not os.path.isdir(directory):
        return 0
    removed = sum(len(load_json(path)) for path in glob.glob(os.path.join(directory, "*.json")))
    shutil.rmtree(directory)
    return removed


def cmd_clear(args):
    """Reset the cumulative total. By default only today's (other days kept), or
    every day with --all (which also frees the on-disk state directory).

    Resetting today sets today's total back to 0, and if a prompt was blocked by
    hitting the limit, the next prompt goes through. In-progress sessions then
    count only new usage from this point on (already-counted messages are not
    re-added)."""
    extra = [arg for arg in args if arg != "--all"]
    if extra:
        sys.stderr.write("usage: jiseishin.py clear [--all]\n")
        return 1

    if "--all" in args:
        if not os.path.isdir(STATE_ROOT):
            print(f"[jiseishin] No state files to delete (all days: {STATE_ROOT})")
            return 0
        file_count = sum(len(files) for _root, _dirs, files in os.walk(STATE_ROOT))
        shutil.rmtree(STATE_ROOT)
        print(f"[jiseishin] Deleted all state files ({file_count} file(s), {STATE_ROOT})")
        return 0

    today = datetime.date.today().isoformat()
    removed = clear_today()
    print(f"[jiseishin] Reset today's ({today}) cost: dropped {removed} message record(s). "
          "In-progress sessions count only new usage from here.")
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
