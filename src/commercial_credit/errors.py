"""Typed errors for the commercial credit blueprint.

Every failure the API can surface is one of these, so the HTTP layer
maps them mechanically and tests assert on types, not message text.
"""


class CreditError(Exception):
    """Base class for all blueprint errors."""


class NotFoundError(CreditError):
    """A credit file, document or extraction batch does not exist."""


class ValidationError(CreditError):
    """Input failed a domain rule (as opposed to a schema parse)."""


class ProvenanceError(CreditError):
    """An extraction value arrived without complete provenance.

    Law 1 of the design brief: a figure that cannot be traced to a
    document, page and line/cell does not enter the credit file. The
    extraction port raises this; nothing catches-and-continues it.
    """


class DocumentIntegrityError(CreditError):
    """Stored document bytes no longer match their recorded SHA-256."""


class MemoVerificationError(CreditError):
    """A draft memo failed the figure verifier and was refused storage.

    Carries the offending figures (as rendered) and any policy
    exceptions the draft omitted, so the refusal names exactly what
    could not be traced or was left out — a memo the verifier cannot
    clear never becomes a stored draft.
    """

    def __init__(
        self,
        message: str,
        *,
        untraceable: list[str] | None = None,
        missing_exceptions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.untraceable = untraceable or []
        self.missing_exceptions = missing_exceptions or []


class StoreUnavailableError(CreditError):
    """The system-of-record store cannot be reached or migrated."""
