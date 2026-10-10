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


def send_briefing(markdown: str) -> bool:
    """Send the short daily note (dips, new names, thesis breaks). Empty input stays quiet."""
    text = (markdown or "").strip()
    if not text:
        return False
    return send(markdownify(text))


# Skip reasons worth surfacing (a would-be trade that didn't happen); idempotent
# already-handled skips (2-segment "side:ticker") are routine noise and stay hidden.
_SKIP_LABELS = {
    "unfunded": "budget",
    "not_held": "not held",
    "bad_size": "bad size",
    "bad_qty": "bad size",
    "review_failed": "review failed",
}


def _fmt_placed(o: dict) -> str:
    """One order line with side, ticker, size ($ for buys, shares for sells) and fill state."""
    side = (o.get("side") or "").upper()
    arrow = "🟢" if o.get("side") == "buy" else "🔴"
    if o.get("side") == "buy":
        size = f"${o['dollars']:,.2f}" if o.get("dollars") else ""
    else:
        shares = o.get("shares")
        size = f"{shares:g} sh" if shares else ""
    status = o.get("status")
    bits = [b for b in (size, status) if b]
    tail = f" — {' · '.join(bits)}" if bits else ""
    return f"{arrow} {side} {o.get('ticker')}{tail}"


def _account_footer(acct: dict, placed: list) -> str | None:
    """Sleeve state line: equity · net P&L% · drawdown · cash reserve% · deployed/budget."""
    if not acct:
        return None
    parts = []
    if acct.get("equity") is not None:
        parts.append(f"${acct['equity']:,.0f} equity")
    if acct.get("net_pnl_pct") is not None:
        parts.append(f"{acct['net_pnl_pct']:+.1f}% P&L")
    dd = acct.get("drawdown_pct")
    if dd is not None and dd < 0:
        parts.append(f"{dd:.1f}% drawdown")
    if acct.get("cash_pct") is not None:
        parts.append(f"{acct['cash_pct']:.0f}% cash")
    deployed = sum(o.get("dollars") or 0 for o in placed if o.get("side") == "buy")
    budget = acct.get("budget")
    if deployed and budget:
        parts.append(f"${deployed:,.0f} deployed of ${budget:,.0f}")
    elif deployed:
        parts.append(f"${deployed:,.0f} deployed")
    return "💰 _Sleeve: " + " · ".join(parts) + "_" if parts else None


def notify_exploration(candidates: list[dict], themes_explored: int, already_tracked: int) -> bool:
    """Alert on the weekly discovery run: the new candidates ASTRA found (ticker, theme,
    conviction, rationale gist) or an explicit "nothing new" note so a quiet run is still
    visible on the phone instead of only a GitHub Action notification.

    Always sends. The daily note carries the one-line reason for the names worth reading;
    this message only says that the search ran.
    """
    n = len(candidates)
    screened = (
        f"Screened {themes_explored} theme{'s' if themes_explored != 1 else ''}; "
        f"{already_tracked} already known."
    )
    if n == 0:
        md = f"**Exploration.** No new names this week. {screened}"
        return send(markdownify(md))
    names = ", ".join((c.get("ticker") or "?") for c in candidates[:8])
    extra = f" and {n - 8} more" if n > 8 else ""
    md = (
        f"**Exploration.** {n} new name{'s' if n != 1 else ''}: {names}{extra}. "
        f"The daily note will mention up to two you don't already hold. {screened}"
    )
    return send(markdownify(md))


def notify_agent(summary: dict) -> bool:
    """Alert on an Autotrader run: real orders placed (with size + fill state), blocks
    (with the guardrail reason), and the resulting sleeve state; halt/abort/pause first.

    Only sends when something noteworthy happened (an order, a block, or a halt/pause)
    so routine no-op runs stay quiet. Dry-run placements are flagged as such.
    """
    placed = summary.get("placed") or []
    blocked = summary.get("blocked") or []
    failed = summary.get("failed") or []
    reasons = summary.get("block_reasons") or {}
    acct = summary.get("account") or {}
    dry = summary.get("dry_run")
    footer = f"[Open dashboard →]({DASHBOARD_URL})"

    if summary.get("aborted") == "account_read_failed":
        md = ("⚠️ **ASTRA Autotrader could not run** — the agentic Robinhood account couldn't be "
              "read. Most likely the OAuth token expired; re-authorize with "
              "`python -m src.agent_broker --bootstrap`. No trades until fixed.")
        return send(markdownify(md))
    if summary.get("aborted"):
        md = f"⚠️ **ASTRA Autotrader could not run** — {summary['aborted']}. No trades this run."
        return send(markdownify(md))
    if summary.get("halted"):
        acct_line = _account_footer(acct, placed)
        md = ("🛑 **ASTRA Autotrader HALTED** — drawdown limit breached. Autonomous trading "
              "stopped; manual reset required.")
        if acct_line:
            md += f"\n{acct_line}"
        return send(markdownify(md))
    if "paused" in (summary.get("skipped") or []):
        return False  # paused is an intentional owner action — no alert needed

    # `failed` counts as noteworthy: an order the brain wanted was rejected by Robinhood. Without
    # it, a run where every placement failed had no placed/blocked entries and went out silently.
    if not placed and not blocked and not failed:
        return False  # nothing to report

    tag = " _(dry-run)_" if dry else ""
    lines = [f"🤖 **ASTRA Autotrader**{tag}"]
    lines += [_fmt_placed(o) for o in placed]
    for b in blocked:
        reason = reasons.get(b)
        ticker = b.split(":", 1)[-1]
        lines.append(f"⛔ {ticker} blocked — {reason}" if reason else f"⛔ {ticker} blocked")

    # Robinhood rejected the order (account gate, transient broker error, …). Distinct from a
    # `blocked` guardrail decision: ASTRA wanted this trade and could not get it placed.
    for f in failed:
        side, _, ticker = f.partition(":")
        lines.append(f"❌ {ticker or f} {side} FAILED to place")
    if failed and not placed:
        lines.append("_no orders placed this run — check the workflow log_")

    # Muted line for would-be trades that didn't happen (budget ran out, nothing to sell, …);
    # idempotent already-handled skips are omitted as routine.
    skips = []
    for s in (summary.get("skipped") or []):
        segs = s.split(":")
        if len(segs) >= 3 and segs[2] in _SKIP_LABELS:
            skips.append(f"{segs[1]} ({_SKIP_LABELS[segs[2]]})")
    if skips:
        lines.append(f"_skipped: {', '.join(skips)}_")

    acct_line = _account_footer(acct, placed)
    if acct_line:
        lines.append(acct_line)
    lines.append(footer)
    return send(markdownify("\n".join(lines)))


# ── Self-evaluation (src/scorecard.py) ────────────────────────────────────────
_VERDICT_EMOJI = {
    "SCALE": "📈", "TUNE": "🔧", "REVIEW": "🧠", "STEADY": "➖", "INSUFFICIENT_DATA": "⏳",
}
_VERDICT_HEADLINE = {
    "SCALE": "the record supports more capital",
    "TUNE": "good picks, weak execution",
    "REVIEW": "the picks themselves are lagging",
    "STEADY": "nothing stands out either way",
    "INSUFFICIENT_DATA": "too early to tell",
}
_TRACK_LABEL = {"paper": "Advisor (paper)", "autotrader": "Autotrader (real money)"}


def _pct(value: float | None, scale: float = 1.0, places: int = 1) -> str:
    return "n/a" if value is None else f"{value * scale:+.{places}f}%"


def notify_scorecard(result: dict, previous_verdict: str | None = None) -> bool:
    """Alert when ASTRA's own verdict on its track record CHANGES — never on a repeat.

    A daily "still not enough data" ping would be worthless and would train the alert to be
    ignored, costing exactly the signal this exists to deliver. `scorecard.should_notify` owns
    the decision; this function only formats, so a caller that reaches it has already decided
    the transition is worth sending.

    Every figure is reported with its uncertainty. A verdict here is an argument for moving real
    money, and a bare point estimate at these sample sizes would overstate what is known.
    """
    verdict = result.get("verdict", "")
    track = _TRACK_LABEL.get(result.get("track", ""), result.get("track", "?"))
    emoji = _VERDICT_EMOJI.get(verdict, "•")
    footer = f"[Open dashboard →]({DASHBOARD_URL})"

    lines = [f"{emoji} **ASTRA Scorecard — {track}**"]
    transition = f"{previous_verdict} → {verdict}" if previous_verdict else verdict
    lines.append(f"_{transition} — {_VERDICT_HEADLINE.get(verdict, '')}_")

    if verdict == "INSUFFICIENT_DATA":
        missing = result.get("missing") or []
        if missing:
            lines.append("Still waiting on: " + ", ".join(missing))
        if result.get("eta_days"):
            lines.append(f"At the current rate, about {result['eta_days']} more days.")
    else:
        lines.append(
            f"Execution: {_pct(result.get('exec_cum_excess_adj'), 100)} vs the S&P after "
            f"allowing for uninvested cash "
            f"({int(result.get('nav_days') or 0)} paired days, "
            f"{_pct(result.get('exec_ci_low'), 100)} … {_pct(result.get('exec_ci_high'), 100)})"
        )
        hit = result.get("pick_hit_rate")
        hit_txt = f"{hit * 100:.0f}% beat the S&P" if hit is not None else "hit rate n/a"
        lines.append(
            f"Picks: {hit_txt} over {int(result.get('pick_window_days') or 0)} days "
            f"({int(result.get('distinct_tickers') or 0)} companies, "
            f"avg {_pct(result.get('pick_clustered_excess'))} excess)"
        )

    for reason in (result.get("reasons") or []):
        lines.append(f"• {reason}")

    if verdict == "TUNE":
        for fp in ((result.get("diagnostic") or {}).get("fingerprints") or []):
            lines.append(f"🔍 _{fp.get('name')}_: {fp.get('detail')}")
        # Load-bearing: this module's whole promise is that it observes and never self-tunes.
        lines.append("_No settings were changed. This is a read-only observation._")

    lines.append(footer)
    return send(markdownify("\n".join(lines)))
