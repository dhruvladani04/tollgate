"""An LLM judge behind the same decision interface as Jev.

TypeSafe's `system-one-adapter` asks a general LLM the same questions in the
same shape, so the judge needs no prompt of its own. Each judge is pinned to
one model and one thinking level, because both change its answers.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import typesafe_sdk as sdk
from system_one_adapter import AsyncSystemOneAdapterClient
from system_one_adapter.providers.base import ProviderResult, record_request, translating
from system_one_adapter.providers.gemini import AsyncGeminiProvider, _request_kwargs, _result

from tollgate.decide.base import LLM_API_KEY_ENV, Decision, DecisionError, DecisionRequest
from tollgate.decide.jev import _answer

API_KEY_ENV = LLM_API_KEY_ENV


@dataclass(frozen=True)
class Judge:
    name: str
    """How the judge is named in cache keys, audit rows and reports."""
    model: str
    """The provider's model id."""
    thinking: str | None
    """The thinking level to ask for, or None to send none."""
    corrective_retries: int
    """How many times a malformed answer is sent back to be corrected."""


JUDGES: dict[str, Judge] = {
    # "minimal" is rejected by this model; "low" is the least it accepts.
    "gemini-3.8-flash": Judge("gemini-3.8-flash/thinking-low", "gemini-3.8-flash", "low", 1),
    "gemma-4-31b": Judge("gemma-4-31b-it", "gemma-4-31b-it", None, 3),
}


class LlmConfigError(RuntimeError):
    """The environment or the judge name does not describe a usable judge."""


def resolve_judge(name: str) -> Judge:
    if name not in JUDGES:
        raise LlmConfigError(f"{name!r} is not a known judge (one of: {', '.join(sorted(JUDGES))})")
    return JUDGES[name]


def read_api_key(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    key = env.get(API_KEY_ENV, "").strip()
    if not key:
        raise LlmConfigError(f"{API_KEY_ENV} is not set, and the LLM judge needs it.")
    return key


class _GeminiJudgeProvider(AsyncGeminiProvider):
    """The adapter's Gemini provider, with a thinking level and thinking tokens counted.

    Google bills thinking tokens as output but reports them apart, and the
    adapter's own provider leaves them out.
    """

    def __init__(self, judge: Judge, *, api_key: str) -> None:
        super().__init__(judge.model, api_key=api_key)
        self._thinking = judge.thinking

    async def request(
        self, messages: list[Any], *, schema: dict[str, Any], structured: bool
    ) -> ProviderResult:
        with translating(self.translate_error):
            kwargs = _request_kwargs(self.model_name, messages, schema, structured=structured)
            if self._thinking is not None:
                kwargs["generation_config"] = {"thinking_level": self._thinking}
            record_request(kwargs, api="interactions")
            response = await self._client.aio.interactions.create(**kwargs)
        result = _result(response)
        thought = getattr(response.usage, "total_thought_tokens", None) or 0
        return ProviderResult(
            text=result.text,
            input_tokens=result.input_tokens,
            output_tokens=(result.output_tokens or 0) + thought,
        )


class LlmDecisionModel:
    """Asks an LLM judge through the adapter. Close it with `aclose` when done."""

    backend = "llm"

    def __init__(self, judge: Judge, provider: Any, *, retry: sdk.RetryPolicy | None = None):
        self.model = judge.name
        self._provider = provider
        self._client = AsyncSystemOneAdapterClient(
            structured_outputs=True,
            llm_answer_mode="probabilities",
            normalize_probabilities=True,
            n_retry_malformed_structure=judge.corrective_retries,
            retry=retry,
        )

    @classmethod
    def for_judge(
        cls,
        judge: Judge,
        *,
        env: Mapping[str, str] | None = None,
        retry: sdk.RetryPolicy | None = None,
    ) -> "LlmDecisionModel":
        provider = _GeminiJudgeProvider(judge, api_key=read_api_key(env))
        return cls(judge, provider, retry=retry)

    async def decide(self, request: DecisionRequest) -> Decision:
        try:
            response = await self._client.system_one(
                dict(request.state), dict(request.questions), model=self._provider
            )
        except sdk.TypeSafeAPITimeoutError as error:
            raise DecisionError("timeout", str(error)) from error
        except sdk.TypeSafeAPIConnectionError as error:
            raise DecisionError("unreachable", str(error)) from error
        except sdk.TypeSafeRateLimitError as error:
            raise DecisionError("rate_limited", str(error)) from error
        except sdk.TypeSafeError as error:
            raise DecisionError("failed", str(error)) from error

        unanswered = sorted(set(request.questions) - set(response.answers))
        if unanswered:
            raise DecisionError("failed", f"the judge did not answer: {', '.join(unanswered)}")
        return Decision(
            answers={name: _answer(response.answers[name]) for name in request.questions},
            model=self.model,
            # Totals across corrective retries, because every attempt is billed.
            input_tokens=response.usage.input_tokens_total,
            output_tokens=response.usage.output_tokens_total,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        close = getattr(self._provider, "aclose", None)
        if close is not None:
            await close()
