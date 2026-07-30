"""Usage accounting — what each recording actually consumed.

Two DIFFERENT kinds of consumption, deliberately kept apart:

  * **api** — real money leaving a real account. OpenAI transcription, and the
    wake adjudicator. Bounded by a hard monthly budget: when the month's api
    spend reaches it, transcription STOPS rather than continuing to charge.

  * **subscription** — the agent. `claude -p` authenticates with the operator's
    Claude Code OAuth credential, so a run consumes rate-limit quota against the
    subscription and bills nothing per token. The CLI still reports an imputed
    `total_cost_usd`, which is useful for efficiency comparisons and useless as a
    bill. Tracked for reporting only, and NEVER counted against the budget.

Conflating the two is the trap this module exists to avoid: an earlier version of
the pipeline's budget error called subscription usage "$8 spent", which is simply
not what happened.

The ledger is `.state/usage-<host>.jsonl` in the vault — append-only, one file
per host, exactly like the seen ledger, so two hosts never conflict on a rebase.
"""
import json
import os
from datetime import UTC, datetime
from pathlib import Path

API, SUBSCRIPTION = "api", "subscription"
# Bookkeeping events that are neither money nor quota — currently just the record
# of which budget alerts have already fired. Excluded from every total, so a
# marker can live in the same append-only ledger without skewing a report.
META = "meta"

# OpenAI list prices, USD per minute of audio. Overridable, because a price
# change should not require a code edit to keep the accounting honest.
STT_USD_PER_MINUTE = {
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
}
# USD per 1M tokens, for the adjudicator's chat-completions call.
CHAT_USD_PER_MTOK = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


def _host() -> str:
    import re
    import socket
    h = os.environ.get("ATTICUS_HOST") or socket.gethostname()
    return re.sub(r"[^a-z0-9-]+", "-", h.lower().split(".")[0]) or "unknown"


def ledger_path(vault: Path) -> Path:
    return Path(vault) / ".state" / f"usage-{_host()}.jsonl"


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def month_key(when: datetime | None = None) -> str:
    """UTC calendar month. Deliberately not a rolling 30-day window: a monthly
    budget the operator can reason about should reset on a date they can name."""
    d = when or datetime.now(UTC)
    return f"{d.year:04d}-{d.month:02d}"


def transcription_usd(seconds: float, model: str) -> float:
    rate = STT_USD_PER_MINUTE.get(model, 0.006)
    return round((max(0.0, seconds) / 60.0) * rate, 6)


def chat_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    inp, out = CHAT_USD_PER_MTOK.get(model, (0.15, 0.60))
    return round(prompt_tokens / 1e6 * inp + completion_tokens / 1e6 * out, 6)


def record(vault: Path, *, kind: str, billing: str, stem: str = "",
           model: str = "", usd: float = 0.0, log=None, **extra) -> dict:
    """Append one usage event. Never raises — accounting must not break a run.

    `billing` decides whether this counts against the budget, so it is required
    rather than inferred: a caller that has to name it cannot accidentally file
    subscription tokens as money.
    """
    if billing not in (API, SUBSCRIPTION, META):
        raise ValueError(f"billing must be one of {API!r}, {SUBSCRIPTION!r}, "
                         f"{META!r} — got {billing!r}")
    event = {"at": _utcnow(), "month": month_key(), "kind": kind,
             "billing": billing, "stem": stem, "model": model,
             "usd": round(float(usd or 0.0), 6), "host": _host()}
    event.update({k: v for k, v in extra.items() if v is not None})
    try:
        p = ledger_path(vault)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:
        if log:
            log(f"    ! could not record usage: {e}")
    return event


def load(vault: Path, month: str | None = None) -> list[dict]:
    """Every usage event, from every host's ledger. Unparseable lines are
    skipped rather than fatal — a truncated write must not blind the report."""
    state = Path(vault) / ".state"
    if not state.is_dir():
        return []
    out = []
    for p in sorted(state.glob("usage-*.jsonl")):
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if month is None or ev.get("month") == month:
                out.append(ev)
    return out


def api_spend(vault: Path, month: str | None = None) -> float:
    """Real money this month. Subscription usage is excluded BY DESIGN."""
    return round(sum(e.get("usd", 0.0) for e in load(vault, month or month_key())
                     if e.get("billing") == API), 6)


def budget_state(vault: Path, cfg) -> dict:
    """Where the month stands against the api budget.

    A budget of 0 or less disables the cap — reported as unlimited rather than
    as an instantly-exhausted budget, which would stop the pipeline dead.
    """
    budget = float(getattr(cfg, "api_budget_usd", 0) or 0)
    spent = api_spend(vault)
    return {
        "month": month_key(),
        "budget_usd": budget,
        "spent_usd": spent,
        "remaining_usd": round(budget - spent, 6) if budget > 0 else None,
        "enabled": budget > 0,
        "exhausted": budget > 0 and spent >= budget,
    }


def newly_crossed(vault: Path, cfg) -> list[float]:
    """Alert thresholds this month's api spend has passed and not yet announced.

    "Not yet announced" is recorded in the LEDGER, not in a notify stamp file.
    notify()'s throttle is a time window (default 6h), which is the wrong shape
    here: once spend is over a threshold it stays over, so a time-based throttle
    would re-announce the same crossing every 6 hours for the rest of the month.
    A ledger marker is month-scoped, survives a cleared /tmp, is committed with
    everything else, and resets by itself when the month rolls over.

    Returns ascending thresholds so a single pass that jumps two of them
    announces both, in order, rather than silently skipping one.
    """
    thresholds = sorted(float(t) for t in getattr(cfg, "budget_alert_usd", []) or [])
    if not thresholds:
        return []
    spent = api_spend(vault)
    already = {round(float(e.get("threshold_usd") or 0), 4)
               for e in load(vault, month_key())
               if e.get("kind") == "budget-alert"}
    return [t for t in thresholds
            if spent >= t and round(t, 4) not in already]


def mark_alerted(vault: Path, threshold: float, spent: float, log=None):
    """Record that a threshold has been announced, so it is announced once."""
    record(vault, kind="budget-alert", billing=META,
           threshold_usd=round(float(threshold), 4),
           spent_usd=round(float(spent), 6), log=log)


def summarise(vault: Path, month: str | None = None) -> dict:
    """Report shape: api money by kind, subscription tokens by model."""
    events = load(vault, month or month_key())
    api = {}
    for e in (x for x in events if x.get("billing") == API):
        row = api.setdefault(e.get("kind", "?"), {"calls": 0, "usd": 0.0,
                                                  "seconds": 0.0})
        row["calls"] += 1
        row["usd"] = round(row["usd"] + e.get("usd", 0.0), 6)
        row["seconds"] += float(e.get("audio_seconds") or 0)

    sub = {}
    for e in (x for x in events if x.get("billing") == SUBSCRIPTION):
        row = sub.setdefault(e.get("model") or "?", {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "web_searches": 0, "imputed_usd": 0.0})
        row["calls"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "web_searches"):
            row[k] += int(e.get(k) or 0)
        row["imputed_usd"] = round(row["imputed_usd"] + e.get("usd", 0.0), 6)

    return {"month": month or month_key(), "api": api, "subscription": sub,
            "api_total_usd": round(sum(r["usd"] for r in api.values()), 6),
            "subscription_imputed_usd": round(
                sum(r["imputed_usd"] for r in sub.values()), 6),
            # Consumption events only. META markers (budget alerts already sent)
            # live in the same ledger and would otherwise inflate this count.
            "events": sum(1 for e in events
                          if e.get("billing") in (API, SUBSCRIPTION))}


def from_claude_json(payload: dict) -> dict:
    """Normalise `claude -p --output-format json` into ledger fields.

    The CLI reports per-model detail under `modelUsage` and a roll-up under
    `usage`. Prefer the roll-up for totals and take the model name from the
    largest `modelUsage` entry, since a run can touch more than one model (a
    small one for routing plus the main one for the work).
    """
    usage = payload.get("usage") or {}
    models = payload.get("modelUsage") or {}
    main = ""
    if models:
        main = max(models, key=lambda m: (models[m] or {}).get("outputTokens", 0))
        main = (models[main] or {}).get("canonicalModel") or main
    tools = usage.get("server_tool_use") or {}
    return {
        "model": main,
        "usd": float(payload.get("total_cost_usd") or 0.0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "web_searches": int(tools.get("web_search_requests") or 0)
                        + int(tools.get("web_fetch_requests") or 0),
        "turns": int(payload.get("num_turns") or 0),
        "duration_ms": int(payload.get("duration_ms") or 0),
    }
