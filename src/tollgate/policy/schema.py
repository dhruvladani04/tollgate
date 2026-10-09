"""The policy file: every number the gate decides with, loaded from YAML.

No section has a default. A policy file must state every threshold, so that no
number hides in code.
"""

import re
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from tollgate.decide.questions import RISK

# The risk score runs from 0 to the index of its highest level.
RISK_TOP = len(RISK["criteria"]) - 1

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
RiskScore = Annotated[float, Field(ge=0.0, le=RISK_TOP)]


class PolicyError(Exception):
    """The policy file is missing, unreadable or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Thresholds(_Section):
    risk_ask: RiskScore
    """A risk score at or above this holds the call for a person."""
    risk_block: RiskScore
    """A risk score above this blocks the call when it is also off task."""
    destructive: Probability
    """A probability above this holds the call for a person."""
    exfiltration: Probability
    """A probability above this blocks the call."""
    on_task: Probability
    """A probability below this means the call is off task."""
    confidence: Probability
    """An answer less confident than this is escalated instead of acted on."""

    @model_validator(mode="after")
    def _ask_level_is_not_above_block_level(self) -> Self:
        if self.risk_ask > self.risk_block:
            raise ValueError("risk_ask must not be above risk_block")
        return self


class ThresholdOverrides(_Section):
    risk_ask: RiskScore | None = None
    risk_block: RiskScore | None = None
    destructive: Probability | None = None
    exfiltration: Probability | None = None
    on_task: Probability | None = None
    confidence: Probability | None = None


class DecisionSettings(_Section):
    timeout_ms: int = Field(gt=0)
    """Budget for one decision, retries included. Past it, the call is held for a person."""
    max_retries: int = Field(ge=0)


class PathSandbox(_Section):
    roots: list[str]
    """Folders that path arguments must stay inside. An empty list turns the check off."""
    arguments: list[str]
    """Names of the tool arguments that hold a path, or a list of paths."""


class Prefilter(_Section):
    allow_tools: list[str]
    """Tools that are always allowed, without asking the model."""
    deny_tools: list[str]
    """Tools that are always blocked."""
    max_argument_bytes: int = Field(gt=0)
    """Arguments larger than this are not sent to the model; a person decides."""
    secret_patterns: dict[str, str]
    """Named regular expressions. A match blocks the call and is redacted from the log."""
    path_sandbox: PathSandbox

    @field_validator("secret_patterns")
    @classmethod
    def _patterns_compile(cls, patterns: dict[str, str]) -> dict[str, str]:
        for name, pattern in patterns.items():
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"{name} is not a valid regular expression: {error}") from error
        return patterns


class ApprovalSettings(_Section):
    timeout_seconds: int = Field(gt=0)
    """A held call that nobody answers is denied after this long."""


class ToolPolicy(_Section):
    thresholds: ThresholdOverrides = ThresholdOverrides()


class Policy(_Section):
    decision: DecisionSettings
    thresholds: Thresholds
    prefilter: Prefilter
    approval: ApprovalSettings
    tools: dict[str, ToolPolicy] = Field(default_factory=dict)
    """Per-tool overrides of the global thresholds."""

    def thresholds_for(self, tool: str) -> Thresholds:
        override = self.tools.get(tool)
        if override is None:
            return self.thresholds
        changes = override.thresholds.model_dump(exclude_none=True)
        return Thresholds.model_validate(self.thresholds.model_dump() | changes)

    @model_validator(mode="after")
    def _overrides_give_valid_thresholds(self) -> Self:
        for tool in self.tools:
            try:
                self.thresholds_for(tool)
            except ValidationError as error:
                raise ValueError(f"tools.{tool}: {error.errors()[0]['msg']}") from error
        return self


def load_policy(path: Path) -> Policy:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise PolicyError(f"cannot read the policy file {path}: {error.strerror}") from error
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise PolicyError(f"{path} is not valid YAML: {error}") from error
    return parse_policy(document)


def parse_policy(document: Any) -> Policy:
    if not isinstance(document, dict):
        raise PolicyError("the policy file must be a mapping of sections")
    try:
        return Policy.model_validate(document)
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        where = ".".join(map(str, first["loc"])) or "policy"
        raise PolicyError(f"{where}: {first['msg']}") from error
