"""
Keep the conviction list in step with trades Abhi already made.

The daily run asks, once, when a name he no longer holds is still on the list,
or when he buys a name that is not on it. He replies `drop DAL` / `keep DAL` /
`add XYZ` / `skip XYZ`. Replies are read at the start of the next run. There is
no webhook: the weekday job polls Telegram, which is the only process we run.
"""

from __future__ import annotations

import copy
import logging
import re
from datetime import datetime, timezone

import requests

from src import config
from src.brain.conviction import is_excluded
from src.briefing import known_tickers

logger = logging.getLogger(__name__)

PROMPT_LIMIT = 3
_CMD = re.compile(r"^(drop|keep|add|skip)\s+([A-Za-z][A-Za-z0-9.]{0,9})$", re.IGNORECASE)
_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


def parse_command(text: str) -> tuple[str, str] | None:
    """`drop DAL` → ('drop', 'DAL'). Anything else is not a reply."""
    cleaned = (text or "").strip().strip("`")
    match = _CMD.match(cleaned)
    if not match:
        return None
    return match.group(1).lower(), match.group(2).upper()


def drop_candidates(convictions: dict, held: set[str], sold: set[str]) -> list[str]:
    """Names to ask about removing.

    An individual holding he no longer owns, or a theme name he sold and no longer
    owns. Frozen themes are one decision (EV, via Tesla), not a pile of tickers.
    Names already marked do-not-add are not asked again.
    """
    owned = {t.upper() for t in held}
    sold_u = {t.upper() for t in sold}
    found: set[str] = set()
    for ticker, holding in (convictions.get("individual_holdings") or {}).items():
        if not isinstance(holding, dict) or holding.get("status") == "do_not_add":
            continue
        if ticker.upper() not in owned:
            found.add(ticker.upper())
    for theme in (convictions.get("themes") or {}).values():
        if not isinstance(theme, dict) or theme.get("frozen"):
            continue
        for bucket in ("preferred", "approved", "hold_only"):
            for ticker in theme.get(bucket) or []:
                name = str(ticker).upper()
                if name not in owned and name in sold_u:
                    found.add(name)
    return sorted(found)


def add_candidates(convictions: dict, held: set[str], bought: set[str]) -> list[str]:
    """Buys of a name that is still held and that he has never put on the list."""
    owned = {t.upper() for t in held}
    known = known_tickers(convictions)
    return sorted(t.upper() for t in bought if t.upper() in owned and t.upper() not in known)


def drop_ticker(convictions: dict, ticker: str) -> dict:
    """Move `ticker` to do-not-add wherever it is listed. Idempotent."""
    updated = copy.deepcopy(convictions)
    name = ticker.upper()
    holdings = updated.setdefault("individual_holdings", {})
    if isinstance(holdings.get(name), dict):
        holdings[name]["status"] = "do_not_add"
        holdings[name]["action"] = "Dropped after you sold it."
    for theme in (updated.get("themes") or {}).values():
        if not isinstance(theme, dict):
            continue
        moved = False
        for bucket in ("preferred", "approved", "hold_only"):
            bucket_names = theme.get(bucket) or []
            if name in bucket_names:
                theme[bucket] = [t for t in bucket_names if t != name]
                moved = True
        if moved:
            blocked = theme.setdefault("do_not_add", [])
            if name not in blocked:
                blocked.append(name)
    _touch(updated, f"Dropped {name} from a Telegram reply.")
    return updated


def add_ticker(convictions: dict, ticker: str) -> dict | None:
    """Record a buy as an individual holding. None when the name is hard-excluded."""
    if is_excluded(ticker.upper(), convictions):
        return None
    updated = copy.deepcopy(convictions)
    holdings = updated.setdefault("individual_holdings", {})
    holdings[ticker.upper()] = {
        "status": "approved",
        "action": "Added because you bought it.",
        "thesis": "",
    }
    _touch(updated, f"Added {ticker.upper()} from a Telegram reply.")
    return updated


def decide(
    convictions: dict,
    kind: str,
    ticker: str,
    pending: set[tuple[str, str]],
) -> tuple[dict, str | None, str]:
    """Apply one reply.

    Returns (convictions, new prompt status or None when nothing was pending, reply text).
    `drop`/`keep` answer a drop question. `add`/`skip` answer an add question.
    """
    name = ticker.upper()
    if kind in ("drop", "keep") and ("drop", name) not in pending:
        return convictions, None, f"No open question for {name}."
    if kind in ("add", "skip") and ("add", name) not in pending:
        return convictions, None, f"No open question for {name}."
    if kind == "keep":
        return convictions, "kept", f"Keeping {name} on the list."
    if kind == "skip":
        return convictions, "skipped", f"Left {name} off the list."
    if kind == "drop":
        return drop_ticker(convictions, name), "dropped", f"Dropped {name}. It will not be bought again."
    added = add_ticker(convictions, name)
    if added is None:
        return convictions, "skipped", f"{name} stays off the list."
    return added, "added", f"Added {name} to the list."


def format_prompts(pending: list[dict]) -> str:
    """The question block appended to the daily note. Empty when nothing is waiting."""
    if not pending:
        return ""
    lines = ["**Your list**"]
    shown = pending[:PROMPT_LIMIT]
    for row in shown:
        ticker = row["ticker"]
        if row["kind"] == "drop":
            lines.append(
                f"You no longer hold **{ticker}**, and it is still on your list. "
                f"Reply `drop {ticker}` or `keep {ticker}`."
            )
        else:
            lines.append(
                f"You bought **{ticker}**, which is not on your list. "
                f"Reply `add {ticker}` or `skip {ticker}`."
            )
    extra = len(pending) - len(shown)
    if extra > 0:
        lines.append(f"{extra} more waiting.")
    lines.append("Replies are applied on the next morning run.")
    return "\n".join(lines)


def _touch(convictions: dict, note: str) -> None:
    meta = convictions.setdefault("_meta", {})
    meta["last_updated"] = datetime.now(timezone.utc).date().isoformat()
    meta["notes"] = note


# ---------------------------------------------------------------------------
# I/O. Failures stay in the caller; this module logs and returns quietly only
# from the two entry points the daily run calls.
# ---------------------------------------------------------------------------

def _client():
    from src.db import get_client
    return get_client()


def _rows(query) -> list[dict]:
    from src.db import rows
    return rows(query)


def list_prompts() -> list[dict]:
    return _rows(
        _client().table("owner_prompts").select("ticker, kind, status, created_at")
        .order("created_at").execute().data
    )


def _insert_prompt(ticker: str, kind: str) -> None:
    _client().table("owner_prompts").insert({"ticker": ticker, "kind": kind, "status": "pending"}).execute()


def _resolve(ticker: str, kind: str, status: str) -> None:
    _client().table("owner_prompts").update({
        "status": status,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("ticker", ticker).eq("kind", kind).execute()


def _cursor() -> int:
    data = _rows(_client().table("telegram_cursor").select("update_offset").eq("id", 1).limit(1).execute().data)
    if not data:
        return 0
    return int(data[0].get("update_offset") or 0)


def _set_cursor(offset: int) -> None:
    _client().table("telegram_cursor").upsert({"id": 1, "update_offset": offset}).execute()


def _filled_sides() -> tuple[set[str], set[str]]:
    """(bought tickers, sold tickers) from ingested main-account fills."""
    bought: set[str] = set()
    sold: set[str] = set()
    rows = _rows(
        _client().table("user_orders").select("ticker, side, state").eq("state", "filled")
        .limit(1000).execute().data
    )
    for row in rows:
        ticker = (row.get("ticker") or "").upper()
        if not ticker:
            continue
        if row.get("side") == "buy":
            bought.add(ticker)
        elif row.get("side") == "sell":
            sold.add(ticker)
    return bought, sold


def ensure_trade_prompts(convictions: dict, held: set[str]) -> list[dict]:
    """Create any missing questions and return the pending ones, oldest first."""
    bought, sold = _filled_sides()
    existing = {(row["ticker"], row["kind"]) for row in list_prompts()}
    for ticker in drop_candidates(convictions, held, sold):
        if ("drop", ticker) not in existing:
            _insert_prompt(ticker, "drop")
    for ticker in add_candidates(convictions, held, bought):
        if ("add", ticker) not in existing:
            _insert_prompt(ticker, "add")
    return [row for row in list_prompts() if row.get("status") == "pending"]


def _fetch_updates(token: str, offset: int) -> list[dict]:
    resp = requests.get(
        _UPDATES_URL.format(token=token),
        params={"offset": offset, "timeout": 0, "allowed_updates": ["message"]},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description") or "telegram getUpdates failed")
    return body.get("result") or []


def apply_replies() -> None:
    """Read replies since the last run and update the conviction list. Never raises."""
    token, chat_id = config.telegram.bot_token, str(config.telegram.chat_id or "")
    if not token or not chat_id:
        return
    try:
        updates = _fetch_updates(token, _cursor())
    except Exception:
        logger.error("Could not read Telegram replies — convictions unchanged", exc_info=True)
        return
    if not updates:
        return

    from src import notify
    from src.data_layer import load_convictions
    from src.memory import save_conviction_snapshot

    try:
        convictions = load_convictions()
        pending = {
            (row["ticker"], row["kind"])
            for row in list_prompts() if row.get("status") == "pending"
        }
    except Exception:
        logger.error("Could not load convictions to apply replies", exc_info=True)
        return

    replies: list[str] = []
    resolved: list[tuple[str, str, str]] = []
    changed = False
    max_id = 0
    for update in updates:
        max_id = max(max_id, int(update.get("update_id") or 0))
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id") or "") != chat_id:
            continue
        parsed = parse_command(message.get("text") or "")
        if parsed is None:
            continue
        kind, ticker = parsed
        convictions, status, reply = decide(convictions, kind, ticker, pending)
        replies.append(reply)
        if status is None:
            continue
        question = "drop" if kind in ("drop", "keep") else "add"
        pending.discard((ticker, question))
        resolved.append((ticker, question, status))
        if status in ("dropped", "added"):
            changed = True

    # Save the list before marking questions done. A failure here leaves the
    # cursor unmoved, so the next run tries the same replies again.
    if changed:
        try:
            save_conviction_snapshot(convictions)
        except Exception:
            logger.error("Saving conviction replies failed", exc_info=True)
            return
    for ticker, kind, status in resolved:
        try:
            _resolve(ticker, kind, status)
        except Exception:
            logger.error("Could not resolve prompt %s %s", kind, ticker, exc_info=True)
            return
    try:
        _set_cursor(max_id + 1)
    except Exception:
        logger.error("Could not advance the Telegram cursor", exc_info=True)
        return
    for reply in replies:
        notify.send_briefing(reply)
