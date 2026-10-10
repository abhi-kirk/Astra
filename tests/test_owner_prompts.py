"""Conviction questions: which names to ask about, and what a reply does. No network."""

from src.owner_prompts import (
    add_candidates,
    add_ticker,
    decide,
    drop_candidates,
    drop_ticker,
    format_prompts,
    parse_command,
)


def _convictions():
    return {
        "exclusions": [{"ticker": "TSLA", "reason": "job"}],
        "individual_holdings": {
            "DAL": {"status": "hold", "action": "Hold.", "thesis": "Travel demand."},
        },
        "themes": {
            "space": {"approved": ["RKLB"], "do_not_add": ["SPCE"]},
            "ev_transition": {"frozen": True, "hold_only": ["NIO"], "preferred": ["BYDDY"]},
            "core_tech": {"approved": ["NVDA"]},
        },
    }


class TestParse:
    def test_reads_a_reply(self):
        assert parse_command("  drop dal ") == ("drop", "DAL")
        assert parse_command("`add xyz`") == ("add", "XYZ")

    def test_ignores_prose(self):
        assert parse_command("what should I buy") is None


class TestCandidates:
    def test_asks_about_a_sold_holding_and_not_a_name_he_still_owns(self):
        # DAL is an individual holding he sold. RKLB is approved and still held.
        # NIO sits in a frozen theme, so it is not a separate question.
        found = drop_candidates(_convictions(), held={"RKLB", "BYDDY"}, sold={"DAL", "NIO"})
        assert found == ["DAL"]

    def test_asks_when_a_theme_name_was_sold(self):
        found = drop_candidates(_convictions(), held=set(), sold={"NVDA"})
        assert "NVDA" in found
        assert "RKLB" not in found  # approved, never sold, just not owned yet

    def test_add_is_only_an_unknown_name_he_still_holds(self):
        found = add_candidates(_convictions(), held={"LMT", "NVDA"}, bought={"LMT", "NVDA", "GONE"})
        assert found == ["LMT"]


class TestEdits:
    def test_drop_moves_the_name_to_do_not_add(self):
        updated = drop_ticker(_convictions(), "DAL")
        assert updated["individual_holdings"]["DAL"]["status"] == "do_not_add"

    def test_drop_a_theme_name(self):
        updated = drop_ticker(_convictions(), "NVDA")
        assert "NVDA" not in updated["themes"]["core_tech"]["approved"]
        assert "NVDA" in updated["themes"]["core_tech"]["do_not_add"]

    def test_add_refuses_tesla(self):
        assert add_ticker(_convictions(), "TSLA") is None

    def test_add_records_an_individual_holding(self):
        updated = add_ticker(_convictions(), "LMT")
        assert updated["individual_holdings"]["LMT"]["status"] == "approved"

    def test_reply_without_a_question_does_nothing(self):
        convictions = _convictions()
        updated, status, reply = decide(convictions, "drop", "DAL", pending=set())
        assert status is None
        assert updated == convictions
        assert "No open question" in reply

    def test_drop_reply_updates_and_keep_does_not(self):
        convictions = _convictions()
        updated, status, _reply = decide(convictions, "drop", "DAL", {("drop", "DAL")})
        assert status == "dropped"
        assert updated["individual_holdings"]["DAL"]["status"] == "do_not_add"
        kept, status, reply = decide(convictions, "keep", "DAL", {("drop", "DAL")})
        assert status == "kept"
        assert kept["individual_holdings"]["DAL"]["status"] == "hold"
        assert "Keeping DAL" in reply


class TestFormat:
    def test_shows_three_and_counts_the_rest(self):
        pending = [{"ticker": t, "kind": "drop"} for t in ("DAL", "KO", "BA", "NOK")]
        text = format_prompts(pending)
        assert "drop DAL" in text and "drop BA" in text
        assert "NOK" not in text
        assert "1 more waiting" in text
        assert "next morning run" in text

    def test_empty_is_empty(self):
        assert format_prompts([]) == ""
