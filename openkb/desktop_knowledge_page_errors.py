"""Stable domain errors shared by Desktop Knowledge Page modules."""


class DesktopKnowledgePageError(RuntimeError):
    """A stable domain error for Desktop Knowledge Page operations."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
