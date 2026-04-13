from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .config import OutputCompressionConfig


DESTRUCTIVE_MARKERS = ("rm ", "del ", "remove-item", "git reset", "git checkout --", "drop ", "truncate ", "shutdown", "reboot")
SECURITY_MARKERS = ("secret", "token", "credential", "password", "auth", "jwt", "oauth", "api key", "private key")
LEGAL_MEDICAL_FINANCIAL_MARKERS = ("legal", "medical", "financial", "compliance", "diagnosis", "tax", "regulation")
AMBIGUITY_MARKERS = ("unclear", "ambiguous", "not sure", "unsure", "which one", "clarify")
STEP_BY_STEP_MARKERS = ("step by step", "step-by-step", "exact steps", "walk me through")


@dataclass
class EffectiveOutputPolicy:
    enabled: bool
    mode: str
    style: str
    level: str
    task_type: str
    operation_kind: str
    detail_requested: bool = False
    bypassed: bool = False
    bypass_reason: str | None = None
    prompt_contract: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _task_text(task: dict[str, Any] | None) -> str:
    if not task:
        return ""
    title = str(task.get("title", "") or "")
    description = str(task.get("description", "") or "")
    tags = " ".join(str(item) for item in (task.get("tags") or []))
    return f"{title} {description} {tags}".strip().lower()


def infer_task_type(
    task: dict[str, Any] | None,
    *,
    operation_kind: str = "general",
    command: str | None = None,
) -> str:
    normalized_operation = (operation_kind or "general").strip().lower()
    if normalized_operation in {"review", "debugging", "architecture", "dangerous_actions"}:
        return normalized_operation
    command_text = (command or "").strip().lower()
    if any(marker in command_text for marker in DESTRUCTIVE_MARKERS):
        return "dangerous_actions"

    text = _task_text(task)
    if any(term in text for term in ("review", "finding", "regression", "code review")):
        return "review"
    if any(term in text for term in ("bug", "debug", "failure", "error", "investigate")):
        return "debugging"
    if any(term in text for term in ("architecture", "design", "system", "refactor")):
        return "architecture"
    return "general"


def detect_safety_bypass(
    config: OutputCompressionConfig,
    *,
    task: dict[str, Any] | None,
    operation_kind: str = "general",
    command: str | None = None,
    detail_requested: bool = False,
) -> str | None:
    if detail_requested and config.expand_on_request:
        return "detail_requested"
    if not config.safety_bypass.enabled:
        return None

    normalized_operation = (operation_kind or "general").strip().lower()
    command_text = (command or "").strip().lower()
    combined_text = f"{normalized_operation} {command_text} {_task_text(task)}".strip()

    if config.safety_bypass.destructive_actions:
        if normalized_operation == "dangerous_actions" or any(marker in command_text for marker in DESTRUCTIVE_MARKERS):
            return "destructive_actions"
    if config.safety_bypass.security_sensitive:
        if normalized_operation == "security_sensitive" or any(marker in combined_text for marker in SECURITY_MARKERS):
            return "security_sensitive"
    if config.safety_bypass.legal_medical_financial:
        if normalized_operation in {"legal_medical_financial", "legal", "medical", "financial"} or any(
            marker in combined_text for marker in LEGAL_MEDICAL_FINANCIAL_MARKERS
        ):
            return "legal_medical_financial"
    if config.safety_bypass.ambiguity_clarification:
        if normalized_operation == "ambiguity_clarification" or any(marker in combined_text for marker in AMBIGUITY_MARKERS):
            return "ambiguity_clarification"
    if config.safety_bypass.step_by_step_sensitive:
        if normalized_operation == "step_by_step_sensitive" or any(marker in combined_text for marker in STEP_BY_STEP_MARKERS):
            return "step_by_step_sensitive"
    return None


def build_prompt_only_contract(
    config: OutputCompressionConfig,
    *,
    task_type: str,
) -> str:
    prompt_only = config.prompt_only
    lines = ["## Response Style Contract", ""]
    if prompt_only.direct_answer_first:
        lines.append("- Start with the answer, result, or recommendation.")
    if prompt_only.no_greetings:
        lines.append("- Skip greetings and conversational filler.")
    if prompt_only.no_recap:
        lines.append("- Do not restate the question or add a closing recap unless it changes meaning.")
    if prompt_only.short_paragraphs:
        lines.append(f"- Keep paragraphs short, usually at most {prompt_only.max_paragraph_sentences} sentences.")
    if prompt_only.prefer_bullets_for_lists:
        lines.append("- Use bullets only when listing distinct items or steps.")
    lines.append("- Preserve code, commands, paths, numbers, errors, and concrete technical facts.")
    if task_type == "review" and prompt_only.findings_first_for_reviews:
        lines.append("- For reviews, lead with findings and risks before summary.")
    elif task_type == "debugging":
        lines.append("- For debugging, state root cause, fix, and risk without extra narration.")
    elif task_type == "architecture":
        lines.append("- For architecture questions, keep the recommendation compact but include the key tradeoff.")
    if config.style == "terse_technical":
        lines.append("- Prefer dense technical phrasing over conversational explanation.")
    elif config.style == "ultra_terse":
        lines.append("- Be extremely brief unless precision would suffer.")
    return "\n".join(lines).strip() + "\n"


def build_gateway_contract(
    config: OutputCompressionConfig,
    *,
    task_type: str,
) -> str:
    gateway = config.gateway_enforced
    lines = [
        "## Enforced Response Contract",
        "",
        f"- Keep the response within roughly {gateway.max_output_tokens_soft} output tokens when possible.",
        f"- Use no more than {gateway.max_output_sections} major sections.",
        f"- Keep paragraphs to about {gateway.max_paragraph_lines} lines.",
    ]
    if gateway.enforce_direct_answer_first:
        lines.append("- Put the answer first.")
    if task_type == "review" and gateway.enforce_findings_first_for_reviews:
        lines.append("- For reviews, findings must come before any overview.")
    lines.append("- Preserve critical technical detail even when shortening phrasing.")
    return "\n".join(lines).strip() + "\n"


def resolve_output_policy(
    config: OutputCompressionConfig,
    *,
    task: dict[str, Any] | None = None,
    operation_kind: str = "general",
    detail_requested: bool = False,
    command: str | None = None,
) -> EffectiveOutputPolicy:
    task_type = infer_task_type(task, operation_kind=operation_kind, command=command)
    mode = config.mode if config.enabled else "off"
    style = config.style
    level = config.level

    override = config.task_overrides.get(task_type)
    if override:
        if override.mode is not None:
            mode = override.mode
        if override.style is not None:
            style = override.style
        if override.level is not None:
            level = override.level

    bypass_reason = detect_safety_bypass(
        config,
        task=task,
        operation_kind=operation_kind,
        command=command,
        detail_requested=detail_requested,
    )
    bypassed = bypass_reason is not None
    effective_mode = "off" if bypassed else mode

    contract = ""
    if effective_mode == "prompt_only":
        contract = build_prompt_only_contract(config, task_type=task_type)
    elif effective_mode == "gateway_enforced":
        contract = build_gateway_contract(config, task_type=task_type)

    return EffectiveOutputPolicy(
        enabled=effective_mode != "off",
        mode=effective_mode,
        style=style,
        level=level,
        task_type=task_type,
        operation_kind=(operation_kind or "general").strip().lower(),
        detail_requested=detail_requested,
        bypassed=bypassed,
        bypass_reason=bypass_reason,
        prompt_contract=contract,
        metadata={
            "configured_mode": config.mode,
            "configured_style": config.style,
            "configured_level": config.level,
        },
    )
