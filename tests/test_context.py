from tollgate.context import CallContext, trim, trimmed


def test_a_short_text_is_left_alone():
    assert trim("Tidy my notes", 100) == "Tidy my notes"
    assert trim("exactly ten", 11) == "exactly ten"


def test_a_long_text_keeps_its_start_and_its_end():
    text = "Summarise this log. " + "x" * 1000 + " Then save it."

    cut = trim(text, 40)

    assert cut.startswith("Summarise this log. ")
    assert cut.endswith(" Then save it.")
    assert f"[... {len(text) - 40} characters left out ...]" in cut


def test_trimming_a_context_cuts_the_request_and_every_turn():
    context = CallContext(user_request="r" * 500, recent_context=("a" * 500, "short"))

    cut = trimmed(context, 20)

    assert len(cut.user_request) < 80
    assert len(cut.recent_context[0]) < 80
    assert cut.recent_context[1] == "short"


def test_trimming_a_context_without_a_request_keeps_it_without_one():
    assert trimmed(CallContext(), 20) == CallContext()
