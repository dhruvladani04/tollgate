from fakes import tool

from tollgate.decide.questions import (
    RECENT_CONTEXT_META,
    USER_REQUEST_META,
    CallContext,
    call_request,
    context_from_meta,
)

READ = tool("read_text_file", "Read a file as text.")


def test_without_a_user_request_only_the_call_is_sent_and_judged():
    request = call_request(READ, {"path": "notes.txt"}, CallContext())

    assert request.state == {
        "proposed_call": {
            "tool": "read_text_file",
            "description": "Read a file as text.",
            "arguments": {"path": "notes.txt"},
        }
    }
    assert set(request.questions) == {"risk", "destructive"}


def test_with_a_user_request_the_questions_that_need_it_are_added():
    context = CallContext(user_request="Summarise notes.txt")

    request = call_request(READ, {"path": "notes.txt"}, context)

    assert request.state["user_request"] == "Summarise notes.txt"
    assert set(request.questions) == {"risk", "destructive", "on_task", "exfiltration"}


def test_state_never_holds_more_than_the_three_fields_the_spec_allows():
    context = CallContext(user_request="Tidy up", recent_context=("a", "b", "c"))

    request = call_request(READ, {"path": "x"}, context)

    assert list(request.state) == ["user_request", "proposed_call", "recent_context"]


def test_recent_context_keeps_only_the_last_two_turns():
    context = CallContext(user_request="Tidy up", recent_context=("first", "second", "third"))

    request = call_request(READ, None, context)

    assert request.state["recent_context"] == ["second", "third"]


def test_a_call_without_arguments_or_description_still_has_a_complete_proposed_call():
    request = call_request(tool("ping"), None, CallContext())

    assert request.state["proposed_call"] == {"tool": "ping", "description": "", "arguments": {}}


def test_every_question_names_the_state_field_it_reads_in_backticks():
    context = CallContext(user_request="Tidy up")

    request = call_request(READ, None, context)

    for name, question in request.questions.items():
        assert "`proposed_call`" in question["instructions"], name
    for name in ("on_task", "exfiltration"):
        assert "`user_request`" in request.questions[name]["instructions"], name


def test_risk_has_the_four_ordered_levels_and_the_rest_are_yes_or_no():
    context = CallContext(user_request="Tidy up")

    questions = call_request(READ, None, context).questions

    assert questions["risk"]["type"] == "score"
    assert len(questions["risk"]["criteria"]) == 4
    assert {questions[name]["type"] for name in ("destructive", "on_task", "exfiltration")} == {
        "noul"
    }


def test_context_is_read_from_the_tollgate_meta_keys():
    context = context_from_meta(
        {
            USER_REQUEST_META: "Summarise notes.txt",
            RECENT_CONTEXT_META: ["I opened the folder.", "It has one file."],
            "progress_token": 7,
        }
    )

    assert context == CallContext(
        user_request="Summarise notes.txt",
        recent_context=("I opened the folder.", "It has one file."),
    )


def test_missing_or_malformed_meta_gives_an_empty_context():
    assert context_from_meta(None) == CallContext()
    assert context_from_meta({}) == CallContext()
    assert context_from_meta({USER_REQUEST_META: "", RECENT_CONTEXT_META: "x"}) == CallContext()
    assert context_from_meta({USER_REQUEST_META: 5, RECENT_CONTEXT_META: [1, "kept"]}) == (
        CallContext(recent_context=("kept",))
    )
