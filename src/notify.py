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


def format_message(buy_tickers, sell_tickers, advisor_note, mode="simulation") -> str:
    """Compose the run message as Markdown and convert it to Telegram MarkdownV2.

    No date/title line: the bot name ("ASTRA Signals") and Telegram's own timestamp
    already supply that, so that scarce banner space goes to the advisor note instead.
    Buy/sell use 🟢/🔴 — Telegram text can't be colored, so a colored emoji is the only
    way to carry the green/red cue (arrow/chart emoji are monochrome or mis-colored).
    """
    buys = ", ".join(buy_tickers) if buy_tickers else "none"
    sells = ", ".join(sell_tickers) if sell_tickers else "none"

    head = []
    if mode and mode != "simulation":
        head.append(f"_mode: {mode}_")
    head += [f"🟢 **BUY:** {buys}", f"🔴 **SELL:** {sells}"]
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
    token, chat_id = config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID
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


def notify_run(buy_tickers, sell_tickers, advisor_note, mode="simulation") -> bool:
    """Format and send the daily run notification."""
    return send(format_message(buy_tickers, sell_tickers, advisor_note, mode))
