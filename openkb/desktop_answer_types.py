"""Wire-safe values for Desktop grounded retrieval and completed answers."""

from __future__ import annotations

from dataclasses import dataclass

from openkb.desktop_retrieval_trace import DesktopRetrievalTrace


class DesktopAnswerError(RuntimeError):
    """A stable domain error for the Desktop grounded-answer path."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DesktopRetrievalPlan:
    """The bounded query terms used to build one auditable Evidence Pack."""

    query: str
    terms: tuple[str, ...]
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "terms": list(self.terms),
            "source": self.source,
        }


@dataclass(frozen=True)
class DesktopEvidenceRef:
    """One stable original-document fragment selected for an answer."""

    evidence_id: str
    document_id: str
    document_name: str
    section: str
    locator: dict[str, object]
    excerpt: str
    channels: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "section": self.section,
            "locator": self.locator,
            "excerpt": self.excerpt,
            "channels": list(self.channels),
        }


@dataclass(frozen=True)
class DesktopAnswerSourceImage:
    """One retained original image explicitly associated with a cited evidence fragment."""

    source_image_id: str
    evidence_id: str
    document_id: str
    document_name: str
    name: str
    media_type: str
    file_path: str
    alt_text: str | None
    locator: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "source_image_id": self.source_image_id,
            "evidence_id": self.evidence_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "name": self.name,
            "media_type": self.media_type,
            "file_path": self.file_path,
            "alt_text": self.alt_text,
            "locator": self.locator,
        }


@dataclass(frozen=True)
class DesktopEvidencePack:
    """The source-only context passed to the optional answer model."""

    retrieval_plan: DesktopRetrievalPlan
    evidence: tuple[DesktopEvidenceRef, ...]
    degradations: tuple[str, ...] = ()
    source_images: tuple[DesktopAnswerSourceImage, ...] = ()
    retrieval_trace: DesktopRetrievalTrace = DesktopRetrievalTrace()


@dataclass(frozen=True)
class DesktopGroundedAnswer:
    """A persisted completed or interrupted answer and its cited source fragments."""

    answer_id: str
    question: str
    answer_text: str
    retrieval_plan: DesktopRetrievalPlan
    citations: tuple[DesktopEvidenceRef, ...]
    degradations: tuple[str, ...]
    created_at: str
    source_images: tuple[DesktopAnswerSourceImage, ...] = ()
    retrieval_trace: DesktopRetrievalTrace = DesktopRetrievalTrace()
    status: str = "completed"
    interruption_code: str | None = None
    interruption_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "answer_id": self.answer_id,
            "question": self.question,
            "answer_text": self.answer_text,
            "retrieval_plan": self.retrieval_plan.as_dict(),
            "citations": [citation.as_dict() for citation in self.citations],
            "degradations": list(self.degradations),
            "created_at": self.created_at,
            "source_images": [image.as_dict() for image in self.source_images],
            "retrieval_trace": self.retrieval_trace.as_dict(),
            "status": self.status,
            "interruption_code": self.interruption_code,
            "interruption_reason": self.interruption_reason,
        }
