"""Model-call and decision metrics, from the LLM tracer's records (2026-10-09).

Every model call — an LLM call or a Jev decisions call — reaches the call's tracer as one
LLMTraceRecord (app/llm/structured.py). This turns a record into metrics:

  llm_latency_ms (+ tokens, cost)   EVERY call, tagged with what it was doing: its ``task`` and
                                    ``task_kind`` (decision | extraction | generation | retrieval |
                                    unclassified), the raw ``call_type`` tag, the model, ``status``
  decision_latency_ms               a call whose task is a DECISION: how long the turn waited for
                                    it (retries and backoff included — the record's latency is
                                    from before the first attempt), tagged ``decision``,
                                    ``decider`` (jev | llm), ``status`` and, when the decision's
                                    answer is from a closed set, ``result``
  decision_outcome                  one per answered question the task names (value 1), tagged
                                    ``decision``, ``question``, ``answer`` — what was decided,
                                    and how often

WHICH call is which task is the domain's to say: a workflow names its task table in its
definition (``"metrics": {"tasks": "<path>.yaml"}``); this module knows only the shape. A tag
the table does not name is still measured, under its own name with kind ``unclassified``.
Platform code: no domain noun here.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DECISION, UNCLASSIFIED = "decision", "unclassified"
# a result or answer becomes a metric label: a short closed-set token, never free text
_LABEL = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")


@dataclass(frozen=True)
class TaskSpec:
    """What one model call (by its tag) is doing. ``result``: the question (Jev) or field (LLM)
    whose answer is the decision's headline; ``outcomes``: the questions/fields counted."""
    task: str
    kind: str = UNCLASSIFIED
    result: str = ""
    outcomes: tuple[str, ...] = field(default_factory=tuple)


def task_for(tag: str, tasks: dict[str, TaskSpec]) -> TaskSpec:
    return tasks.get(tag) or TaskSpec(task=tag or "untagged")


def _label(value) -> str | None:
    """A closed-set answer as a label: booleans as yes/no, a short token as itself; anything
    else (free text, a figure, a name) is 'other' — never a high-cardinality label."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    return text if _LABEL.match(text) else "other"


def _answers(record, decider: str) -> dict:
    """question/field -> answer, from the record's response: Jev's per-question answers (a
    choice, a yes/no probability, a score) or an LLM's JSON object."""
    try:
        parsed = json.loads(record.response) if record.response else {}
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    if decider != "jev":
        return parsed
    answers = {}
    for question, answer in parsed.items():
        if isinstance(answer, dict) and "choice" in answer:
            answers[question] = answer["choice"]
        elif isinstance(answer, dict) and "noul" in answer:
            answers[question] = float(answer["noul"]) >= 0.5
    return answers


def emit_for_record(sink, record, *, workflow_id: str, call_id: str, tasks: dict[str, TaskSpec],
                    cost_usd: float | None, pricing: str) -> None:
    """The metrics of one traced model call (see the module docstring). ``cost_usd`` is the
    caller's pricing of the record (None when it failed: a failed call is timed, not metered)."""
    spec = task_for(record.tag, tasks)
    decider = "jev" if getattr(record, "backend", "") == "jev" else "llm"
    status = "error" if record.error else "ok"
    task_tags = dict(task=spec.task, task_kind=spec.kind, model=record.model, decider=decider, status=status)
    if record.error:
        sink.emit_llm_failure(workflow_id, record.latency_ms, agent_id="", call_type=record.tag, instance_id=call_id,
                              pricing=pricing, **task_tags)
    else:
        sink.emit_llm_call(workflow_id, "", record.tag, call_id, record.latency_ms, record.input_tokens,
                           record.output_tokens, record.cached_tokens, cost_usd or 0.0, pricing, task_tags)
    if spec.kind != DECISION:
        return
    answers = {} if record.error else _answers(record, decider)
    decision_tags = dict(decision=spec.task, decider=decider, status=status, instance_id=call_id)
    result = _label(answers.get(spec.result)) if spec.result else None
    if result is not None:
        decision_tags["result"] = result
    sink.emit_decision(workflow_id, record.latency_ms, **decision_tags)
    for question in spec.outcomes:
        answer = _label(answers.get(question))
        if answer is not None:
            sink.emit_decision_outcome(workflow_id, decision=spec.task, decider=decider,
                                       question=question, answer=answer, instance_id=call_id)
