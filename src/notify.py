"""
Telegram notification of the daily run result.

Posts the signal tally + advisor note to a Telegram chat via the Bot API so the
signals land on the phone without opening the dashboard. The advisor note is
Markdown (the dashboard renders it with marked.js); telegramify-markdown converts
the composed message to Telegram MarkdownV2 so it renders natively in the chat.
Non-fatal by design: any failure is logged and swallowed so a notification error
never breaks the run.
"""

import logging

import requests
from telegramify_markdown import markdownify

from src import config

logger = logging.getLogger(__name__)

DASHBOARD_URL = "https://abhi-kirk.github.io/Astra/"
_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_LEN = 4096       # Telegram hard limit per message
_NOTE_BUDGET = 3000   # cap the raw note so the converted message stays under _MAX_LEN
_GIST_MAX = 220       # length of the preview gist shown high in the message


def _extract_gist(md: str, max_chars: int = _GIST_MAX) -> str:
    """First sentence or two of the note's prose, surfaced high in the message so it
    shows in the phone's collapsed notification. Skips the note's Markdown title,
    horizontal rules, and section headers; clips at a sentence boundary."""
    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or all(c in "-*_ " for c in s):
            continue
        if len(s) <= max_chars:
            return s
        clip = s[:max_chars]
        cut = max(clip.rfind(". "), clip.rfind("! "), clip.rfind("? "))
        if cut != -1:                       # end on the last full sentence that fits
            return clip[:cut + 1].rstrip()
        return s[:max_chars - 1].rstrip() + "…"   # one giant sentence — hard clip
    return ""


def _actionable_buy(state: str | None) -> bool:
    """A buy is fresh/actionable only as a NEW ENTRY or a cooldown-elapsed ADD.
    IN COOLDOWN / AT ADD CAP names still clear the buy bar but are throttled — an
    uptrending conviction name re-fires every run, so these are "on track, no action",
    not fresh buys. Mirrors the advisor-note PRIORITY ACTIONS rule (paper_buy_label)."""
    if not state:
        return True  # unlabeled (e.g. AI-less path) → don't hide it
    return state.startswith("NEW ENTRY") or state.startswith("ADD ")


def format_message(buy_tickers, sell_tickers, advisor_note, mode="simulation", buy_labels=None) -> str:
    """Compose the run message as Markdown and convert it to Telegram MarkdownV2.

    No date/title line: the bot name ("ASTRA Signals") and Telegram's own timestamp
    already supply that, so that scarce banner space goes to the advisor note instead.
    Buy/sell use 🟢/🔴 — Telegram text can't be colored, so a colored emoji is the only
    way to carry the green/red cue (arrow/chart emoji are monochrome or mis-colored).

    `buy_labels` (ticker → pyramiding state) splits the BUY line into genuinely actionable
    buys and still-qualifying-but-throttled repeats, so a conviction name in an uptrend
    stops reading as a fresh buy every single day.
    """
    buy_labels = buy_labels or {}
    actionable = [t for t in buy_tickers if _actionable_buy(buy_labels.get(t))]
    on_track = [t for t in buy_tickers if not _actionable_buy(buy_labels.get(t))]
    buys = ", ".join(actionable) if actionable else "none"
    sells = ", ".join(sell_tickers) if sell_tickers else "none"

    head = []
    if mode and mode != "simulation":
        head.append(f"_mode: {mode}_")
    head += [f"🟢 **BUY:** {buys}", f"🔴 **SELL:** {sells}"]
    if on_track:
        # Muted line: these are held conviction names still passing the buy screen but
        # throttled by pyramiding (recent add / at cap) — surfaced so their absence from BUY
        # doesn't read as "dropped", but clearly not a fresh action.
        head.append(f"⏸️ _on track (no action): {', '.join(on_track)}_")
    header = "\n".join(head)
    footer = f"[Open dashboard →]({DASHBOARD_URL})"

    note = (advisor_note or "").strip()
    if not note:
        md = f"{header}\n\n_No advisor note (AI skipped)._\n\n{footer}"
    else:
        gist = _extract_gist(note)
        if len(note) > _NOTE_BUDGET:
            note = note[:_NOTE_BUDGET].rstrip() + "…"
        # Gist hugs the tally (single newline, no blank line) so it lands within the
        # phone's ~4-line collapsed banner; the full formatted note follows after a gap.
        lead = f"{header}\n{gist}" if gist else header
        md = f"{lead}\n\n{note}\n\n{footer}"

    converted = markdownify(md)
    if len(converted) > _MAX_LEN:
        # Note too long even after the budget — drop it, keep the actionable header.
        converted = markdownify(f"{header}\n\n_Full advisor note on the dashboard._\n\n{footer}")
    return converted


def send(text: str) -> bool:
    """POST a message to Telegram. Returns True on success, False on skip/failure."""
    token, chat_id = config.telegram.bot_token, config.telegram.chat_id
    if not token or not chat_id:
        logger.info("Telegram not configured (TELEGRAM_BOT_TOKEN/CHAT_ID unset) — skipping notification")
        return False
    try:
        resp = requests.post(
            _API_URL.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("Telegram notification sent")
        return True
    except Exception:
        logger.error("Telegram notification failed — pipeline continues", exc_info=True)
        return False


def notify_run(buy_tickers, sell_tickers, advisor_note, mode="simulation", buy_labels=None) -> bool:
    """Format and send the daily run notification."""
    return send(format_message(buy_tickers, sell_tickers, advisor_note, mode, buy_labels))


def notify_agent(summary: dict) -> bool:
    """Alert on an Autotrader run: real orders placed, blocks, halt/pause.

    Only sends when something noteworthy happened (an order, a block, or a halt/pause)
    so routine no-op runs stay quiet. Dry-run placements are flagged as such.
    """
    placed = summary.get("placed") or []
    blocked = summary.get("blocked") or []
    dry = summary.get("dry_run")

    if summary.get("aborted") == "account_read_failed":
        md = ("⚠️ **ASTRA Autotrader could not run** — the agentic Robinhood account couldn't be "
              "read. Most likely the OAuth token expired; re-authorize with "
              "`python -m src.agent_broker --bootstrap`. No trades until fixed.")
        return send(markdownify(md))
    if summary.get("aborted"):
        md = f"⚠️ **ASTRA Autotrader could not run** — {summary['aborted']}. No trades this run."
        return send(markdownify(md))
    if summary.get("halted"):
        md = "🛑 **ASTRA Autotrader HALTED** — drawdown limit breached. Autonomous trading stopped; manual reset required."
        return send(markdownify(md))
    if "paused" in (summary.get("skipped") or []):
        return False  # paused is an intentional owner action — no alert needed

    if not placed and not blocked:
        return False  # nothing to report

    tag = " _(dry-run)_" if dry else ""
    lines = [f"🤖 **ASTRA Autotrader**{tag}"]
    for o in placed:
        arrow = "🟢" if o.get("side") == "buy" else "🔴"
        lines.append(f"{arrow} {o.get('side', '').upper()} {o.get('ticker')}")
    if blocked:
        lines.append(f"⛔ blocked: {', '.join(blocked)}")
    lines.append(f"[Open dashboard →]({DASHBOARD_URL})")
    return send(markdownify("\n".join(lines)))
