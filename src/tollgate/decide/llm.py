"""An LLM judge behind the same decision interface as Jev.

TypeSafe's `system-one-adapter` asks a general LLM the same questions in the
same shape, so the judge needs no prompt of its own. Each judge is pinned to
one provider, one model and one thinking level, because all three change its
answers.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import typesafe_sdk as sdk
from system_one_adapter import AsyncSystemOneAdapterClient
from system_one_adapter.providers.base import ProviderResult, record_request, translating

from tollgate.decide.base import LLM_API_KEY_ENVS, Decision, DecisionError, DecisionRequest
from tollgate.decide.jev import _answer


@dataclass(frozen=True)
class Judge:
    model: str
    """The provider's model id."""
    thinking: str | None
    """The thinking level to ask Gemini for, or None to send none."""
    corrective_retries: int
    """How many times a malformed answer is sent back to be corrected."""
    provider: str = "gemini"

    @property
    def name(self) -> str:
        """How the judge is named in cache keys, audit rows and reports.

        The thinking level changes the answers, so it is part of the name.
        """
        return f"{self.model}/thinking-{self.thinking}" if self.thinking else self.model


# The judges the evaluation uses.
JUDGES: dict[str, Judge] = {
    # "minimal" is rejected by this model; "low" is the least it accepts.
    "gemini-3.8-flash": Judge("gemini-3.8-flash", "low", 1),
    "gemma-4-31b": Judge("gemma-4-31b-it", None, 3),
}


class LlmConfigError(RuntimeError):
    """The environment or the judge does not describe a usable judge."""


def resolve_judge(name: str) -> Judge:
    if name not in JUDGES:
        raise LlmConfigError(f"{name!r} is not a known judge (one of: {', '.join(sorted(JUDGES))})")
    return JUDGES[name]


def read_api_key(judge: Judge, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    variable = LLM_API_KEY_ENVS[judge.provider]
    key = env.get(variable, "").strip()
    if not key:
        raise LlmConfigError(
            f"{variable} is not set, and the second model ({judge.provider}) needs it."
        )
    return key


def _gemini_provider(judge: Judge, api_key: str) -> Any:
    from system_one_adapter.providers.gemini import AsyncGeminiProvider, _request_kwargs, _result

    class GeminiJudgeProvider(AsyncGeminiProvider):
        """The adapter's Gemini provider, with a thinking level and thinking tokens counted.

        Google bills thinking tokens as output but reports them apart, and the
        adapter's own provider leaves them out.
        """

        async def request(
            self, messages: list[Any], *, schema: dict[str, Any], structured: bool
        ) -> ProviderResult:
            with translating(self.translate_error):
                kwargs = _request_kwargs(self.model_name, messages, schema, structured=structured)
                if judge.thinking is not None:
                    kwargs["generation_config"] = {"thinking_level": judge.thinking}
                record_request(kwargs, api="interactions")
                response = await self._client.aio.interactions.create(**kwargs)
            result = _result(response)
            thought = getattr(response.usage, "total_thought_tokens", None) or 0
            return ProviderResult(
                text=result.text,
                input_tokens=result.input_tokens,
                output_tokens=(result.output_tokens or 0) + thought,
            )

    return GeminiJudgeProvider(judge.model, api_key=api_key)


def make_provider(judge: Judge, env: Mapping[str, str] | None = None) -> Any:
    """The adapter provider for a judge. Each provider's SDK is an optional install."""
    api_key = read_api_key(judge, env)
    try:
        if judge.provider == "gemini":
            return _gemini_provider(judge, api_key)
        if judge.provider == "anthropic":
            from system_one_adapter.providers.anthropic import AsyncAnthropicProvider

            return AsyncAnthropicProvider(judge.model)
        from system_one_adapter.providers.openai import AsyncOpenAIProvider

        return AsyncOpenAIProvider(judge.model, api_key=api_key)
    except ImportError as error:
        raise LlmConfigError(
            f"the {judge.provider} provider is not installed. "
            f"Install it with: uv add 'system-one-adapter[{judge.provider}]'"
        ) from error


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
        return cls(judge, make_provider(judge, env), retry=retry)

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
