"""The one path every tool listing and tool call takes on its way to an upstream."""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from mcp import types

from tollgate.approve.queue import ApprovalQueue, ApprovalStatus
from tollgate.audit.models import AuditRecord
from tollgate.audit.store import AuditStore
from tollgate.decide.base import Decision, DecisionError, DecisionModel
from tollgate.decide.cache import request_key
from tollgate.decide.questions import CallContext, call_request, context_from_meta
from tollgate.policy.engine import Band, Verdict, decide_band, no_decision
from tollgate.policy.prefilter import prefilter, redact
from tollgate.policy.schema import Policy

logger = logging.getLogger("tollgate")

CallResult = types.CallToolResult | types.InputRequiredResult


class RoutingError(Exception):
    """A tool could not be routed. `code` is the JSON-RPC error code the client gets."""

    code = types.INTERNAL_ERROR


class UnknownTool(RoutingError):
    code = types.INVALID_PARAMS


class ToolNameCollision(RoutingError):
    pass


class ToolSource(Protocol):
    async def list_tools(self) -> list[types.Tool]: ...

    async def call_tool(self, params: types.CallToolRequestParams) -> CallResult: ...


@dataclass(frozen=True)
class RoutedTool:
    upstream: str
    tool: types.Tool


class ToolRouter:
    """Presents the tools of several upstreams as one list, under their own names."""

    def __init__(self, upstreams: Mapping[str, ToolSource]) -> None:
        self._upstreams = upstreams
        self._tools: dict[str, RoutedTool] = {}

    async def list_tools(self) -> list[types.Tool]:
        """Ask every upstream for its tools and remember which upstream owns each name."""
        tools: dict[str, RoutedTool] = {}
        for upstream_name, upstream in self._upstreams.items():
            for tool in await upstream.list_tools():
                if tool.name in tools:
                    raise ToolNameCollision(
                        f"tool {tool.name!r} is offered by both "
                        f"{tools[tool.name].upstream!r} and {upstream_name!r}"
                    )
                tools[tool.name] = RoutedTool(upstream_name, tool)
        self._tools = tools
        return [routed.tool for routed in tools.values()]

    async def find(self, name: str) -> RoutedTool:
        """The tool with this name and its upstream, listing again if the name is new."""
        if name not in self._tools:
            await self.list_tools()
        routed = self._tools.get(name)
        if routed is None:
            raise UnknownTool(f"Unknown tool: {name}")
        return routed

    async def call_tool(self, params: types.CallToolRequestParams) -> CallResult:
        routed = await self.find(params.name)
        return await self._upstreams[routed.upstream].call_tool(params)


class Gate:
    """Judges every call, records the verdict and, when enforcing, acts on it.

    A call that an exact pre-filter rule decides never reaches the model. Any other
    call is put to the model within the policy's time budget, and the band engine
    turns the answers into a verdict. With no usable answer, a person decides: the
    gate fails closed.

    When not enforcing, the verdict is recorded and the call is forwarded anyway.
    """

    def __init__(
        self,
        router: ToolRouter,
        model: DecisionModel,
        audit: AuditStore,
        policy: Policy,
        approvals: ApprovalQueue | None = None,
        *,
        enforce: bool,
        cwd: Path | None = None,
    ) -> None:
        if enforce and approvals is None:
            raise ValueError("enforcing needs an approval queue for the calls it holds")
        self._router = router
        self._model = model
        self._audit = audit
        self._policy = policy
        self._approvals = approvals
        self._enforce = enforce
        self._cwd = cwd or Path.cwd()

    async def list_tools(self) -> list[types.Tool]:
        return await self._router.list_tools()

    async def call_tool(self, params: types.CallToolRequestParams) -> CallResult:
        routed = await self._router.find(params.name)
        context = context_from_meta(params.meta)
        # Secrets are kept out of the log and out of anything shown to a person.
        arguments = redact(params.arguments, self._policy.prefilter)

        decision: Decision | None = None
        error: str | None = None
        latency_ms: float | None = None
        key: str | None = None
        verdict = prefilter(params.name, params.arguments, self._policy.prefilter, self._cwd)
        if verdict is None:
            request = call_request(routed.tool, params.arguments, context)
            key = request_key(self._model.backend, self._model.model, request)
            budget_ms = self._policy.decision.timeout_ms
            started = time.perf_counter()
            try:
                decision = await asyncio.wait_for(self._model.decide(request), budget_ms / 1000)
            except TimeoutError:
                error = f"timeout: no decision within {budget_ms} ms"
            except DecisionError as failure:
                error = f"{failure.kind}: {failure}"
            except Exception as failure:
                logger.exception("the decision model failed unexpectedly")
                error = f"failed: {failure}"
            latency_ms = (time.perf_counter() - started) * 1000
            verdict = (
                decide_band(decision.answers, self._policy.thresholds_for(params.name))
                if decision is not None
                else no_decision(error or "unknown failure")
            )

        action, approval_id = "forwarded", None
        if self._enforce:
            if verdict.band is Band.BLOCK:
                action = "blocked"
            elif verdict.band is not Band.ALLOW:
                # Escalation to a second model is not built yet, so an uncertain
                # call is held for a person, exactly as an Ask is.
                action, approval_id = await self._hold(routed, arguments, context, verdict)

        record = AuditRecord(
            timestamp=datetime.now(UTC),
            mode="enforce" if self._enforce else "observe",
            band=verdict.band,
            reasons=list(verdict.reasons),
            action=action,
            approval_id=approval_id,
            upstream=routed.upstream,
            tool=params.name,
            arguments=arguments,
            user_request=context.user_request,
            request_key=key,
            backend=self._model.backend if key else None,
            model=decision.model if decision else None,
            cached=decision.cached if decision else False,
            latency_ms=latency_ms,
            input_tokens=decision.input_tokens if decision else None,
            output_tokens=decision.output_tokens if decision else None,
            answers=decision.answers if decision else {},
            error=error,
        )
        try:
            self._audit.write(record)
        except sqlite3.Error:
            logger.exception("could not write the audit record for %s", params.name)
            if self._enforce and action == "forwarded":
                # An enforcing gate does not let through what it cannot record.
                return _refusal(
                    "Tollgate could not record this call, so it was not run. "
                    "Check Tollgate's log for the audit error."
                )

        if action == "forwarded":
            return await self._router.call_tool(params)
        return _refusal(_explain(action, approval_id, verdict, self._policy))

    async def _hold(
        self,
        routed: RoutedTool,
        arguments: dict | None,
        context: CallContext,
        verdict: Verdict,
    ) -> tuple[str, int]:
        """Hold a call until a person answers or time runs out; return what happened."""
        assert self._approvals is not None
        call_id = self._approvals.submit(
            upstream=routed.upstream,
            tool=routed.tool.name,
            arguments=arguments,
            user_request=context.user_request,
            reasons=verdict.reasons,
            timeout_seconds=self._policy.approval.timeout_seconds,
        )
        logger.info(
            "holding %s as #%d for approval: %s",
            routed.tool.name,
            call_id,
            "; ".join(verdict.reasons),
        )
        try:
            status = await self._approvals.wait(call_id)
        except asyncio.CancelledError:
            self._approvals.close_out(call_id, ApprovalStatus.CANCELLED)
            raise
        logger.info("held call #%d was %s", call_id, status)
        return ("forwarded" if status is ApprovalStatus.APPROVED else str(status)), call_id


def _refusal(text: str) -> types.CallToolResult:
    """A tool result that tells the agent the call was not run, and why."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def _explain(action: str, approval_id: int | None, verdict: Verdict, policy: Policy) -> str:
    reasons = "; ".join(verdict.reasons)
    if action == "blocked":
        return f"Blocked by Tollgate: {reasons}. The call was not run."
    if action == "denied":
        return (
            f"A person denied this call through Tollgate (held call #{approval_id}). "
            f"It was held because: {reasons}. The call was not run."
        )
    return (
        f"Tollgate held this call for approval (held call #{approval_id}) and nobody "
        f"approved it within {policy.approval.timeout_seconds} seconds. "
        f"It was held because: {reasons}. The call was not run."
    )
