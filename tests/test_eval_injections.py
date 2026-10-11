"""The result-scan data and its committed answers, plus the pieces the scan is built from."""

from datetime import UTC, datetime

import pytest

from evals.data import CACHE
from evals.injections import jev_key, load_texts, scan_request
from tollgate.audit.models import AuditRecord
from tollgate.decide.cache import DecisionCache
from tollgate.decide.questions import INJECTED, result_request


def test_the_scan_sends_only_the_result_text_and_asks_one_question():
    request = result_request("Version 2.4 fixes a rounding error.")

    assert request.state == {"tool_result": "Version 2.4 fixes a rounding error."}
    assert request.questions == {"injected": INJECTED}
    # The question names the state field it reads, as every question does.
    assert "`tool_result`" in INJECTED["instructions"]


def test_the_committed_texts_keep_the_datasets_own_split_and_have_unique_ids():
    texts = load_texts()

    assert len({entry["id"] for entry in texts}) == len(texts) == 662
    assert sum(entry["split"] == "train" for entry in texts) == 546
    assert sum(entry["split"] == "test" for entry in texts) == 116
    assert 0 < sum(entry["injected"] for entry in texts) < len(texts)


def test_every_text_has_a_cached_answer_to_the_request_the_gate_would_send():
    cache = DecisionCache(CACHE)

    for entry in load_texts():
        assert scan_request(entry) == result_request(entry["text"])
        decision = cache.get(jev_key(entry))
        assert decision is not None, entry["id"]
        assert set(decision.answers) == {"injected"}


def test_a_record_can_be_annotated_after_it_is_written(audit):
    row_id = audit.write(
        AuditRecord(
            timestamp=datetime.now(UTC),
            mode="enforce",
            action="forwarded",
            upstream="web",
            tool="fetch",
        )
    )

    audit.annotate(row_id, result_action="withheld", injected=0.93, withheld_result="do this")

    [row] = audit.recent(1)
    assert (row.result_action, row.injected, row.withheld_result) == ("withheld", 0.93, "do this")
    with pytest.raises(ValueError, match="not audit fields: nonsense"):
        audit.annotate(row_id, nonsense=1)
