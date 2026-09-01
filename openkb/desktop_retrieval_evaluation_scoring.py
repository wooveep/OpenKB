"""Deterministic case and aggregate scoring for retrieval evaluations."""

from __future__ import annotations

import re
from collections.abc import Sequence

from openkb.desktop_answer_types import DesktopEvidencePack, DesktopEvidenceRef
from openkb.desktop_retrieval_channels import DesktopEvaluationVariant
from openkb.desktop_retrieval_evaluation_types import (
    DesktopEvaluationAnswer,
    DesktopEvaluationModelCost,
    DesktopRetrievalEvaluationCase,
    DesktopRetrievalEvaluationCaseResult,
    DesktopRetrievalEvaluationMetrics,
)
from openkb.desktop_retrieval_fusion import BASELINE_EVIDENCE_PACK_LIMIT


def case_result(
    case: DesktopRetrievalEvaluationCase,
    repetition: int,
    variant: DesktopEvaluationVariant,
    expected_evidence_ids: tuple[str, ...],
    pack: DesktopEvidencePack,
    answer: DesktopEvaluationAnswer,
    latency_ms: float,
    retrieval_latency_ms: float,
    answer_latency_ms: float,
    model_cost: DesktopEvaluationModelCost,
    *,
    original_evidence_ids: tuple[str, ...] = (),
) -> DesktopRetrievalEvaluationCaseResult:
    cited_ids = (
        tuple(reference.evidence_id for reference in pack.evidence)
        if answer.cited_evidence_ids is None
        else tuple(dict.fromkeys(answer.cited_evidence_ids))
    )
    cited = set(cited_ids)
    retrieved_at_k = {
        reference.evidence_id for reference in pack.evidence[:BASELINE_EVIDENCE_PACK_LIMIT]
    }
    expected = set(expected_evidence_ids)
    original_expected = set(original_evidence_ids)
    if expected:
        evidence_recall = len(retrieved_at_k & expected) / len(expected)
        citation_precision = len(cited & expected) / len(cited) if cited else 0.0
    else:
        evidence_recall = 1.0 if not retrieved_at_k else 0.0
        citation_precision = 1.0 if not cited else 0.0
    if original_expected:
        original_recall = len(retrieved_at_k & original_expected) / len(original_expected)
        original_citation_precision = len(cited & original_expected) / len(cited) if cited else 0.0
    else:
        original_recall = 1.0 if case.expect_absent_answer and not retrieved_at_k else 0.0
        original_citation_precision = 1.0 if case.expect_absent_answer and not cited else 0.0
    observation = case.original_observation
    normalized_answer = answer.text.casefold()
    answer_points = observation.answer_points if observation is not None else ()
    answer_point_coverage = (
        sum(point.casefold() in normalized_answer for point in answer_points) / len(answer_points)
        if answer_points
        else 1.0
        if case.expect_absent_answer and observation is not None
        else 0.0
    )
    marker_claim_count = (
        sum(
            marker.casefold() in normalized_answer
            for marker in observation.unsupported_claim_markers
        )
        if observation is not None
        else 0
    )
    unsupported_claim_count = max(
        marker_claim_count,
        _unsupported_claim_count(case, answer.text, cited_ids, pack.evidence),
    )
    faithful = _answer_is_faithful(case, expected, cited, answer.text)
    trace = pack.retrieval_trace
    selection_triggered = (
        any(
            channel.channel == "document_page_tree" and bool(channel.trigger_reasons)
            for channel in trace.channels
        )
        and bool(trace.selected_node_ids)
        and any("document_page_tree" in reference.channels for reference in pack.evidence)
    )
    return DesktopRetrievalEvaluationCaseResult(
        case_id=case.case_id,
        category=case.category,
        repetition=repetition,
        variant=variant,
        expected_evidence_ids=expected_evidence_ids,
        evidence_recall_at_k=evidence_recall,
        citation_precision=citation_precision,
        absent_answer_correct=case.expect_absent_answer and faithful,
        answer_faithfulness=1.0 if faithful else 0.0,
        latency_ms=latency_ms,
        retrieval_latency_ms=retrieval_latency_ms,
        answer_latency_ms=answer_latency_ms,
        model_cost=model_cost,
        answer_status=answer.status,
        long_document=case.long_document,
        page_tree_selection_triggered=selection_triggered,
        degradation_reasons=tuple(dict.fromkeys((*pack.degradations, *trace.degradation_reasons))),
        catalog_generation_ids=trace.catalog_generation_ids,
        page_tree_generation_ids=trace.page_tree_generation_ids,
        cited_evidence_ids=cited_ids,
        original_evidence_ids=original_evidence_ids,
        original_evidence_recall_at_k=original_recall,
        original_citation_precision=original_citation_precision,
        original_answer_point_coverage=answer_point_coverage,
        unsupported_claim_count=unsupported_claim_count,
    )


def metrics_for(
    results: list[DesktopRetrievalEvaluationCaseResult], variant: DesktopEvaluationVariant
) -> DesktopRetrievalEvaluationMetrics:
    selected = [result for result in results if result.variant == variant]
    if not selected:
        raise ValueError("Desktop retrieval evaluation has no results for a variant.")
    total = len(selected)
    cost = DesktopEvaluationModelCost()
    for result in selected:
        cost = cost.plus(result.model_cost)
    long_document = [
        result for result in selected if result.long_document and result.category != "absent_answer"
    ]
    absent_answers = [result for result in selected if result.category == "absent_answer"]
    return DesktopRetrievalEvaluationMetrics(
        case_runs=total,
        evidence_recall_k=BASELINE_EVIDENCE_PACK_LIMIT,
        evidence_recall_at_k=sum(result.evidence_recall_at_k for result in selected) / total,
        long_document_evidence_recall_at_k=(
            sum(result.evidence_recall_at_k for result in long_document) / len(long_document)
        ),
        citation_precision=sum(result.citation_precision for result in selected) / total,
        absent_answer_accuracy=(
            sum(result.absent_answer_correct for result in absent_answers) / len(absent_answers)
        ),
        answer_faithfulness=sum(result.answer_faithfulness for result in selected) / total,
        mean_latency_ms=sum(result.latency_ms for result in selected) / total,
        retrieval_p95_ms=p95(tuple(result.retrieval_latency_ms for result in selected)),
        model_cost=cost,
        degradation_runs=sum(bool(result.degradation_reasons) for result in selected),
        original_answer_point_coverage=(
            sum(result.original_answer_point_coverage for result in selected) / total
        ),
        unsupported_claim_count=sum(result.unsupported_claim_count for result in selected),
    )


_CITATION_ORDINAL = re.compile(r"\[(\d+)\]")
_CLAIM_BOUNDARY = re.compile(r"(?:\r?\n+|(?<=[.!?。！？])\s+)")
_ENGLISH_TERM = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.IGNORECASE)
_CJK_TERM = re.compile(r"[\u3400-\u9fff]{2,}")
_ORDERED_LIST_PREFIX = re.compile(r"^\s*\d+[.)、]\s+")
_FACT_LITERAL = re.compile(
    r"https?://[^\s,，。；;]+"
    r"|(?<![\w])(?:--?[a-z][a-z0-9_-]*)(?![\w])"
    r"|(?<![\w])(?:[a-z]:[\\/]|[./]{1,2}[\\/]|/)[^\s,，。；;]+"
    r"|(?<![\w])[a-z_][a-z0-9_.-]*=[^\s,，。；;]+"
    r"|(?<![\w])(?=[a-z0-9@._:-]*\d)(?=[a-z0-9@._:-]*[a-z])"
    r"[a-z][a-z0-9@._:-]*(?![\w])"
    r"|(?<![\w])\d+(?:[.:/-]\d+)*(?![\w])",
    re.IGNORECASE,
)
_CLAIM_STOP_WORDS = frozenset(
    {
        "and",
        "are",
        "for",
        "from",
        "that",
        "the",
        "this",
        "with",
        "即可",
        "以及",
        "可以",
        "需要",
        "进行",
    }
)
_OPERATION_POLARITY_PATTERNS = (
    (
        "enable",
        re.compile(r"\b(?:enable|activate|start|turn\s+on)\b|(?:启用|开启|激活|启动)"),
        re.compile(
            r"\b(?:disable|deactivate|stop|turn\s+off)\b"
            r"|\b(?:do\s+not|don't|must\s+not|never)\s+(?:enable|activate|start)\b"
            r"|(?:禁用|停用|关闭|取消激活|不要启用|不可启用|禁止启用)"
        ),
    ),
    (
        "install",
        re.compile(r"\binstall\b|安装"),
        re.compile(
            r"\b(?:uninstall|remove)\b"
            r"|\b(?:do\s+not|don't|must\s+not|never)\s+install\b"
            r"|(?:卸载|不要安装|不可安装|禁止安装)"
        ),
    ),
    (
        "select",
        re.compile(r"\b(?:select|check)\b|(?:选择|勾选)"),
        re.compile(
            r"\b(?:deselect|unselect|uncheck)\b"
            r"|\b(?:do\s+not|don't|must\s+not|never)\s+(?:select|check)\b"
            r"|(?:取消选择|取消勾选|不要选择|不可选择|禁止选择|不要勾选)"
        ),
    ),
    (
        "retain",
        re.compile(r"\b(?:retain|keep|preserve)\b|(?:保留|保持)"),
        re.compile(r"\b(?:delete|discard)\b|(?:删除|移除|去掉)"),
    ),
)


def cited_evidence_ids(
    answer_text: str, evidence: tuple[DesktopEvidenceRef, ...]
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            evidence[ordinal - 1].evidence_id
            for match in _CITATION_ORDINAL.finditer(answer_text)
            if 0 < (ordinal := int(match.group(1))) <= len(evidence)
        )
    )


def _unsupported_claim_count(
    case: DesktopRetrievalEvaluationCase,
    answer_text: str,
    cited_ids: tuple[str, ...],
    evidence: tuple[DesktopEvidenceRef, ...],
) -> int:
    """Flag novel factual sentences that have no lexical support in cited Evidence."""
    if case.expect_absent_answer and "no available source evidence" in answer_text.casefold():
        return 0
    by_id = {item.evidence_id: item for item in evidence}
    unsupported = 0
    for raw_claim in _CLAIM_BOUNDARY.split(answer_text):
        claim = _ORDERED_LIST_PREFIX.sub("", _CITATION_ORDINAL.sub("", raw_claim)).strip(" #*-\t")
        if not claim:
            continue
        terms = _claim_terms(claim)
        if not terms:
            continue
        ordinals = tuple(
            int(match.group(1))
            for match in _CITATION_ORDINAL.finditer(raw_claim)
            if 0 < int(match.group(1)) <= len(evidence)
        )
        claim_ids = (
            tuple(evidence[ordinal - 1].evidence_id for ordinal in ordinals)
            if ordinals
            else cited_ids
        )
        support = " ".join(
            f"{item.document_name} {item.section} {item.excerpt}"
            for evidence_id in claim_ids
            if (item := by_id.get(evidence_id)) is not None
        ).casefold()
        required = max(1, (len(terms) + 2) // 3)
        exact_literals = tuple(dict.fromkeys(_fact_literals(claim)))
        if (
            any(literal not in support for literal in exact_literals)
            or sum(term in support for term in terms) < required
            or _contradicts_supported_operation(claim, support)
        ):
            unsupported += 1
    return unsupported


def _fact_literals(value: str) -> tuple[str, ...]:
    """Return factual values whose spelling must occur in the cited source."""
    return tuple(match.group(0).rstrip(".)]").casefold() for match in _FACT_LITERAL.finditer(value))


def _claim_terms(value: str) -> tuple[str, ...]:
    english = tuple(
        term
        for match in _ENGLISH_TERM.finditer(value.casefold())
        if (term := match.group(0)) not in _CLAIM_STOP_WORDS
    )
    cjk: list[str] = []
    for match in _CJK_TERM.finditer(value):
        phrase = match.group(0)
        cjk.extend(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return tuple(
        dict.fromkeys((*english, *(term for term in cjk if term not in _CLAIM_STOP_WORDS)))
    )


def _contradicts_supported_operation(claim: str, support: str) -> bool:
    """Detect a cited instruction whose operation polarity is the source's opposite."""
    normalized_claim = claim.casefold()
    normalized_support = support.casefold()
    for _family, positive, negative in _OPERATION_POLARITY_PATTERNS:
        claim_polarities = _operation_polarities(normalized_claim, positive, negative)
        support_polarities = _operation_polarities(normalized_support, positive, negative)
        if len(claim_polarities) == len(support_polarities) == 1:
            if claim_polarities != support_polarities:
                return True
    return False


def _operation_polarities(
    value: str, positive: re.Pattern[str], negative: re.Pattern[str]
) -> frozenset[str]:
    polarities: set[str] = set()
    negative_spans = tuple(match.span() for match in negative.finditer(value))
    if negative_spans:
        polarities.add("negative")
    if any(
        not any(start <= match.start() and match.end() <= end for start, end in negative_spans)
        for match in positive.finditer(value)
    ):
        polarities.add("positive")
    return frozenset(polarities)


def p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Desktop retrieval evaluation latency samples are unavailable.")
    return ordered[max(0, ((95 * len(ordered) + 99) // 100) - 1)]


def _answer_is_faithful(
    case: DesktopRetrievalEvaluationCase,
    expected: set[str],
    cited: set[str],
    answer_text: str,
) -> bool:
    normalized_answer = answer_text.casefold()
    if case.expect_absent_answer:
        return not cited and "no available source evidence" in normalized_answer
    if not expected.issubset(cited):
        return False
    return all(term.casefold() in normalized_answer for term in case.expected_answer_terms)
