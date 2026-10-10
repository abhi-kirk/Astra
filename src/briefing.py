"""
The daily phone note: dips in names already held, and at most a couple of conviction
names Abhi does not already know.

Pure selection and rendering live here. Network (headlines) and Telegram live at the
edges, so the tests never leave the process. Tesla is omitted: the hard exclusion
forbids analyzing it for action, and he can see that position in Robinhood himself.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from src import config

logger = logging.getLogger(__name__)

_HEADLINE_MAX = 140
_RATIONALE_MAX = 160


def select_dips(
    portfolio: dict,
    market_data: dict,
    *,
    threshold: float,
    limit: int,
) -> list[dict]:
    """Holdings at least `threshold` (fraction) below their recent high, deepest first.

    A name with no recent-high figure is skipped rather than guessed from the 52-week high.
    """
    found: list[dict] = []
    for ticker, pos in portfolio.items():
        if float((pos or {}).get("shares") or 0) <= 0:
            continue
        md = market_data.get(ticker) or {}
        pct = md.get("pct_below_3m_high")
        price = md.get("current_price")
        high = md.get("high_3m")
        if pct is None or price is None or high is None:
            continue
        if float(pct) < threshold * 100:
            continue
        found.append({
            "ticker": ticker,
            "drawdown_pct": float(pct),
            "price": float(price),
            "high": float(high),
        })
    found.sort(key=lambda row: -row["drawdown_pct"])
    return found[:limit]


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def pick_new_names(
    candidates: list[dict],
    held: set[str],
    known: set[str],
    *,
    limit: int,
    now: datetime,
    max_age_days: int,
) -> list[dict]:
    """Up to `limit` recent discoveries he does not hold and has not already named.

    `candidates` is newest-first. Older radar names stay in the book for the sleeve
    and are not repeated on the phone every morning.
    """
    picked: list[dict] = []
    blocked = {t.upper() for t in held | known}
    for cand in candidates:
        ticker = (cand.get("ticker") or "").upper()
        if not ticker or ticker in blocked:
            continue
        discovered = _parse_ts(cand.get("discovered_at") or cand.get("updated_at"))
        if discovered is None:
            continue
        if discovered.tzinfo is None:
            discovered = discovered.replace(tzinfo=timezone.utc)
        age = (now - discovered).total_seconds() / 86400
        if age > max_age_days:
            continue
        theme = (cand.get("source_theme") or "").replace("_", " ").strip()
        rationale = _first_sentence(cand.get("rationale") or "", _RATIONALE_MAX)
        picked.append({"ticker": ticker, "theme": theme, "rationale": rationale})
        blocked.add(ticker)
        if len(picked) >= limit:
            break
    return picked


def known_tickers(convictions: dict) -> set[str]:
    """Every ticker he has already named: a theme bucket, a holding note, or an exclusion."""
    names: set[str] = set()
    for ticker in (convictions.get("individual_holdings") or {}):
        names.add(ticker.upper())
    for theme in (convictions.get("themes") or {}).values():
        if not isinstance(theme, dict):
            continue
        for bucket in ("preferred", "approved", "hold_only", "do_not_add"):
            for ticker in theme.get(bucket) or []:
                names.add(str(ticker).upper())
        notes = theme.get("notes")
        if isinstance(notes, dict):
            names.update(str(t).upper() for t in notes)
    for excl in convictions.get("exclusions") or []:
        if isinstance(excl, dict) and excl.get("ticker"):
            names.add(str(excl["ticker"]).upper())
    return names


def thesis_breaks(signals: list[dict]) -> list[dict]:
    """Sell alerts worth texting: the fundamental thesis broke. Chart stops stay quiet."""
    out: list[dict] = []
    for sig in signals:
        if sig.get("action") != "sell" or sig.get("close_reason") != "thesis_invalidation":
            continue
        reasons = [r for r in (sig.get("reasons") or []) if r]
        out.append({"ticker": sig.get("ticker") or "?", "reason": reasons[0] if reasons else "Thesis broke."})
    return out


def _first_sentence(text: str, max_chars: int) -> str:
    s = " ".join((text or "").split())
    if not s:
        return ""
    cut = s.find(". ")
    if cut != -1:
        s = s[:cut + 1]
    if len(s) <= max_chars:
        return s
    return s[:max_chars - 1].rstrip() + "…"


def headline_from_items(items, now_ts: float, max_age_days: int) -> str | None:
    """The newest headline that is still recent enough to explain a dip. None if there isn't one."""
    if not isinstance(items, list):
        return None
    newest: tuple[float, str] | None = None
    for item in items:
        title, published = _headline_fields(item)
        if not title:
            continue
        if published is None:
            published = now_ts
        if now_ts - published > max_age_days * 86400:
            continue
        if newest is None or published > newest[0]:
            newest = (published, title)
    if newest is None:
        return None
    return _first_sentence(newest[1], _HEADLINE_MAX)


def _headline_fields(item) -> tuple[str | None, float | None]:
    if not isinstance(item, dict):
        return None, None
    raw_content = item.get("content")
    content = raw_content if isinstance(raw_content, dict) else {}
    title = item.get("title") or content.get("title")
    published = item.get("providerPublishTime")
    if published is None:
        raw = content.get("pubDate") or item.get("pubDate")
        parsed = _parse_ts(raw) if raw else None
        published = parsed.timestamp() if parsed else None
    try:
        published_f = float(published) if published is not None else None
    except (TypeError, ValueError):
        published_f = None
    text = str(title).strip() if title else None
    return text or None, published_f


def fetch_headline(ticker: str) -> str | None:
    """One recent headline for a dipped holding. Failure returns None; the dip line still sends."""
    try:
        import yfinance as yf
        items = yf.Ticker(ticker).news or []
    except Exception:
        logger.warning("Headline lookup failed for %s", ticker, exc_info=True)
        return None
    return headline_from_items(items, time.time(), config.briefing.headline_max_age_days)


def render_briefing(
    dips: list[dict],
    headlines: dict[str, str | None],
    new_names: list[dict],
    breaks: list[dict],
    *,
    threshold: float,
) -> str:
    """Markdown for the daily Telegram note. No dashboard link: the phone is the interface."""
    pct = round(threshold * 100)
    lines = ["**Dips**"]
    if not dips:
        lines.append(
            f"Nothing you hold is down {pct}% or more from its high of the last three months."
        )
    else:
        for row in dips:
            ticker = row["ticker"]
            line = (
                f"• **{ticker}** is {row['drawdown_pct']:.0f}% below its high of the last "
                f"three months (${row['price']:.2f}, high was ${row['high']:.2f})."
            )
            headline = (headlines or {}).get(ticker)
            if headline:
                line += f" {headline}"
            lines.append(line)

    if new_names:
        lines.append("")
        lines.append("**New**")
        for row in new_names:
            theme = f" ({row['theme']})" if row.get("theme") else ""
            why = f" {row['rationale']}" if row.get("rationale") else ""
            lines.append(f"• **{row['ticker']}**{theme}.{why}")

    if breaks:
        lines.append("")
        lines.append("**Thesis break**")
        for row in breaks:
            lines.append(f"• **{row['ticker']}**: {row['reason']}")

    return "\n".join(lines)
