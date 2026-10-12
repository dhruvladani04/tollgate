"""The dashboard's API, over files written the way the proxy writes them.

The pages themselves are a Next.js app built to static files; these tests cover
the Python side that reads Tollgate's files and answers held calls.
"""

import shutil
from datetime import UTC, datetime

import pytest
from fakes import EXAMPLE_POLICY, decision
from fastapi.testclient import TestClient

from tollgate.approve.queue import APPROVALS_FILE, ApprovalQueue, ApprovalStatus
from tollgate.audit.models import AuditRecord
from tollgate.audit.store import AuditStore
from tollgate.dashboard.app import AUDIT_FILE, STATIC, create_app, describe


def record(**fields) -> AuditRecord:
    base = {
        "timestamp": datetime.now(UTC),
        "mode": "enforce",
        "band": "allow",
        "action": "forwarded",
        "upstream": "files",
        "tool": "read_file",
        "arguments": {"path": "notes.txt"},
    }
    return AuditRecord(**(base | fields))


@pytest.fixture
def state(tmp_path):
    folder = tmp_path / "state"
    folder.mkdir()
    return folder


@pytest.fixture
def policy_file(tmp_path):
    return shutil.copy(EXAMPLE_POLICY, tmp_path / "policy.yaml")


@pytest.fixture
def client(state, policy_file, tmp_path):
    # No built pages here, so the tests do not depend on a Node build.
    return TestClient(create_app(state, policy_file, static_dir=tmp_path / "no-pages"))


def hold(state, **changes) -> int:
    call = {
        "upstream": "files",
        "tool": "delete_file",
        "arguments": {"path": "report.docx"},
        "user_request": "Tidy the folder.",
        "reasons": ["destructive: the call deletes or overwrites data (0.97 is above 0.50)"],
        "timeout_seconds": 300,
    }
    with ApprovalQueue(state / APPROVALS_FILE) as queue:
        return queue.submit(**(call | changes))


def token(client) -> dict[str, str]:
    return {"X-Tollgate-Token": client.get("/api/session").json()["token"]}


# --- what each record is called -------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "outcome", "kind"),
    [
        ({}, "Allowed", "ok"),
        ({"band": "block", "action": "blocked"}, "Blocked", "stopped"),
        ({"band": "ask", "action": "denied", "approval_id": 3}, "Denied by a person", "stopped"),
        (
            {"band": "ask", "action": "expired", "approval_id": 3},
            "Held, nobody answered",
            "stopped",
        ),
        ({"band": "ask", "approval_id": 3}, "Approved by a person", "held"),
        ({"result_action": "withheld", "injected": 0.93}, "Ran; result withheld", "stopped"),
        ({"result_action": "unscanned"}, "Allowed; result not scanned", "warned"),
        ({"mode": "observe", "band": "block"}, "Ran; enforce mode would block", "warned"),
        (
            {"mode": "observe", "result_action": "withheld"},
            "Ran; result would be withheld",
            "warned",
        ),
    ],
)
def test_each_record_gets_a_plain_name_for_what_happened(fields, outcome, kind):
    row = describe(record(**fields))

    assert (row.outcome, row.kind) == (outcome, kind)


# --- reading -------------------------------------------------------------------------


def test_every_endpoint_answers_before_tollgate_has_written_anything(client):
    assert client.get("/api/decisions").json() == {"calls": [], "totals": [], "waiting": 0}
    assert client.get("/api/approvals").json() == {"held": []}
    thresholds = client.get("/api/thresholds").json()
    assert thresholds["problem"] is None and thresholds["judged"] == 0


def test_decisions_are_listed_newest_first_with_outcome_reason_and_scores(client, state):
    with AuditStore(state / AUDIT_FILE) as audit:
        audit.write(record(answers=decision(risk=0.1, on_task=0.95).answers, latency_ms=312.4))
        audit.write(
            record(
                tool="delete_file",
                band="block",
                action="blocked",
                reasons=["path `/etc/passwd` is outside the allowed folders"],
            )
        )

    body = client.get("/api/decisions").json()

    newest, oldest = body["calls"]
    assert (newest["tool"], newest["outcome"], newest["kind"]) == (
        "delete_file",
        "Blocked",
        "stopped",
    )
    assert newest["reasons"] == ["path `/etc/passwd` is outside the allowed folders"]
    assert newest["scores"] == {}
    assert oldest["scores"]["risk"] == 0.1 and oldest["scores"]["on_task"] == 0.95
    assert oldest["latency_ms"] == 312.4
    assert {(total["outcome"], total["count"]) for total in body["totals"]} == {
        ("Blocked", 1),
        ("Allowed", 1),
    }


def test_a_withheld_result_and_its_scan_are_part_of_the_record(client, state):
    with AuditStore(state / AUDIT_FILE) as audit:
        audit.write(
            record(
                tool="fetch",
                result_action="withheld",
                injected=0.9,
                withheld_result="<img src=x onerror=alert(2)>",
            )
        )

    [call] = client.get("/api/decisions").json()["calls"]

    # Sent as data; the pages render it as text, never as markup.
    assert call["withheld_result"] == "<img src=x onerror=alert(2)>"
    assert (call["result_action"], call["injected"]) == ("withheld", 0.9)


def test_thresholds_are_counted_against_recent_calls(client, state):
    with AuditStore(state / AUDIT_FILE) as audit:
        audit.write(record(answers=decision(risk=0.1).answers))
        audit.write(record(answers=decision(risk=1.3).answers))
        audit.write(record(band="ask", answers=decision(risk=2.0).answers))

    body = client.get("/api/thresholds").json()

    by_name = {item["name"]: item for item in body["thresholds"]}
    # The example policy asks at risk 1.4: one of three calls is at or above it,
    # and 1.3 is the nearest that is not.
    risk_ask = by_name["risk_ask"]
    assert (risk_ask["value"], risk_ask["crossed"], risk_ask["judged"]) == (1.4, 1, 3)
    assert risk_ask["nearest"] == 1.3
    assert sorted(risk_ask["scores"]) == [0.1, 1.3, 2.0]
    assert risk_ask["scale_max"] == 3 and by_name["destructive"]["scale_max"] == 1
    assert body["second_model"]["enabled"] is False
    assert body["result_scan"] == {"enabled": True, "injected": 0.5}


def test_a_policy_that_cannot_be_read_is_reported_not_raised(state, tmp_path):
    client = TestClient(
        create_app(state, tmp_path / "absent.yaml", static_dir=tmp_path / "no-pages")
    )

    body = client.get("/api/thresholds").json()

    assert "cannot read the policy file" in body["problem"]


# --- answering held calls -------------------------------------------------------------


def test_a_held_call_is_listed_with_its_reason_and_can_be_approved(client, state):
    call_id = hold(state)

    [held] = client.get("/api/approvals").json()["held"]
    assert (held["id"], held["tool"]) == (call_id, "delete_file")
    assert "deletes or overwrites data" in held["reasons"][0]
    assert held["user_request"] == "Tidy the folder."
    assert 0 < held["seconds_left"] <= 300

    answer = client.post(
        f"/api/approvals/{call_id}", json={"decision": "approve"}, headers=token(client)
    )

    assert answer.json() == {"changed": True, "note": f"Call #{call_id} was approved."}
    with ApprovalQueue(state / APPROVALS_FILE) as queue:
        assert queue.get(call_id).status is ApprovalStatus.APPROVED
        assert queue.pending() == []


def test_a_held_call_can_be_denied(client, state):
    call_id = hold(state)

    client.post(f"/api/approvals/{call_id}", json={"decision": "deny"}, headers=token(client))

    with ApprovalQueue(state / APPROVALS_FILE) as queue:
        assert queue.get(call_id).status is ApprovalStatus.DENIED


def test_a_request_without_the_dashboards_token_cannot_approve_a_call(client, state):
    """What another web page could make the browser send must not be enough."""
    call_id = hold(state)
    body = {"decision": "approve"}

    missing = client.post(f"/api/approvals/{call_id}", json=body)
    forged = client.post(
        f"/api/approvals/{call_id}", json=body, headers={"X-Tollgate-Token": "guess"}
    )

    assert (missing.status_code, forged.status_code) == (403, 403)
    with ApprovalQueue(state / APPROVALS_FILE) as queue:
        assert queue.get(call_id).status is ApprovalStatus.PENDING


def test_a_held_call_cannot_be_approved_by_a_plain_link(client, state):
    call_id = hold(state)

    assert client.get(f"/api/approvals/{call_id}").status_code == 405


def test_only_approve_and_deny_are_accepted(client, state):
    call_id = hold(state)

    answer = client.post(
        f"/api/approvals/{call_id}", json={"decision": "maybe"}, headers=token(client)
    )

    assert answer.status_code == 422


def test_answering_a_call_that_was_already_answered_changes_nothing(client, state):
    call_id = hold(state)
    client.post(f"/api/approvals/{call_id}", json={"decision": "deny"}, headers=token(client))

    again = client.post(
        f"/api/approvals/{call_id}", json={"decision": "approve"}, headers=token(client)
    )

    assert again.json()["changed"] is False
    with ApprovalQueue(state / APPROVALS_FILE) as queue:
        assert queue.get(call_id).status is ApprovalStatus.DENIED


def test_the_number_of_waiting_calls_comes_with_the_decisions(client, state):
    hold(state)
    hold(state, tool="send_email")

    assert client.get("/api/decisions").json()["waiting"] == 2


# --- the pages ------------------------------------------------------------------------


def test_without_built_pages_the_front_page_says_how_to_build_them(client):
    page = client.get("/")

    assert page.status_code == 200
    assert "npm run build" in page.text


def test_the_committed_build_serves_each_page_beside_the_api(state, policy_file):
    if not STATIC.is_dir():
        pytest.skip("the dashboard's pages have not been built")
    client = TestClient(create_app(state, policy_file))

    for path in ("/", "/approvals/", "/thresholds/"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "<title>" in page.text and "Tollgate" in page.text
    assert client.get("/api/approvals").json() == {"held": []}
