from gateway.response_filters import (
    is_autonomous_silence_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
)


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_autonomous_silence_accepts_marker_with_own_line_note():
    """The loose rule for cron/webhook lanes: marker + explanation suppresses."""
    assert is_autonomous_silence_response("[SILENT]")
    assert is_autonomous_silence_response("[SILENT]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[SILENT]")
    assert is_autonomous_silence_response("no_reply\nduplicate inbound, already handled")
    assert is_autonomous_silence_response("[SILENT] No changes detected")


def test_translated_sentinel_is_silence_in_every_form_the_english_one_is():
    """#110935: a lane that answers the cron instruction in its own language translates the
    sentinel; ``[静默]`` must suppress delivery exactly like ``[SILENT]`` (exact, own-line note,
    reordered lines, bracketless, edge punctuation)."""
    assert is_intentional_silence_response("[静默]")
    assert is_intentional_silence_response("**沉默**")
    assert is_autonomous_silence_response("[静默]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[沉默]")
    assert is_autonomous_silence_response("静默")


def test_prose_mentioning_the_translated_sentinel_is_delivered():
    assert not is_intentional_silence_response("status: 静默 means the lane is quiet")
    assert not is_autonomous_silence_response("the lane said 静默 mid-sentence and kept talking")


def test_autonomous_lane_agrees_with_interactive_lane_on_cjk_punctuation_variants():
    """A Chinese lane emits fullwidth brackets or a trailing ``。``; cron/webhook must suppress
    exactly what the interactive predicate suppresses, or the two lanes drift on the new tokens."""
    for variant in ("【静默】", "静默。", "【沉默】", "沉默。", "**[静默]**", "NO_REPLY."):
        assert is_intentional_silence_response(variant)
        assert is_autonomous_silence_response(variant) == is_intentional_silence_response(variant), variant


def test_autonomous_lane_suppresses_one_edit_silence_typo(caplog):
    """#147526: a transposition such as [SLIENT] is one edit, not classic Levenshtein 1.

    Interactive chat stays exact. A short marker-shaped token on its own, or on its own
    first or last line, is silence. Prose that merely contains the typo is delivered.
    """
    import logging

    caplog.set_level(logging.INFO, logger="gateway.response_filters")
    for token in ("[SLIENT]", "SLIENT", "NO RELPY", "[SLIENT]."):
        assert not is_intentional_silence_response(token), token
        assert is_autonomous_silence_response(token), token
    assert is_autonomous_silence_response("inbox unchanged\n\n[SLIENT]")
    assert is_autonomous_silence_response("[SLIENT]\n\nnothing new")
    assert not is_autonomous_silence_response("status: [SLIENT] was the previous leak")
    assert not is_autonomous_silence_response("Silent retry succeeded")
    assert not is_autonomous_silence_response("CLIENT")
    assert not is_autonomous_silence_response("[CLIENT]")
    assert not is_autonomous_silence_response("NOTHING NEW")
    assert not is_autonomous_silence_response("NO PROBLEM")
    assert not is_autonomous_silence_response("静默X")
    assert any("[SLIENT]" in rec.message for rec in caplog.records)
