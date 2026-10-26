"""Typed errors for the commercial credit blueprint.

Every failure the API can surface is one of these, so the HTTP layer
maps them mechanically and tests assert on types, not message text.
"""


class CreditError(Exception):
    """Base class for all blueprint errors."""


class AccessDeniedError(CreditError):
    """An access-policy refusal the caller may see (round 17).

    Carries the policy's coded reason (`role_not_permitted`,
    `readonly_role`, `unknown_role`, `caller_required`) so the
    API answers a coded 403, never a bare one. Refusals that
    must not reveal a file's existence do not use this error —
    they answer with the ordinary NotFoundError instead.
    """

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


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


# ---------------------------------------------------------------------
# Round 15: the model gateway. Profiles resolve from governed data
# or refuse; there is no fallback anywhere in the gateway, so each
# refusal is its own type and the API maps them mechanically.
# ---------------------------------------------------------------------


class ModelGatewayError(CreditError):
    """Base class for every model-gateway refusal."""


class ModelProfileConfigError(ModelGatewayError):
    """The gateway configuration itself is defective: a duplicate
    or unversioned profile set, a malformed or negative price, a
    live profile with no endpoint, a profile whose model has no
    price entry, a malformed budget cap. Raised at load/wiring
    time — a deployment with a broken profile table does not
    start, and no defect is repaired by substituting a profile."""


class UnknownModelProfileError(ModelGatewayError):
    """A caller named a profile the gateway does not hold. No
    profile is substituted for the one that was named."""


class ModelProfileNotConfiguredError(ModelGatewayError):
    """A live profile's endpoint or API key is not set in this
    deployment's environment. The call is refused at wiring or
    call time; no other profile is substituted."""


class ModelResponseError(ModelGatewayError):
    """A live profile returned output the gateway cannot use —
    narrative that does not parse back into the artefact's shape,
    or an extraction payload that is not the contracted JSON. The
    call is still priced and ledgered (it cost what it cost); the
    output is refused, never repaired or partially accepted."""


class ModelBudgetExceededError(ModelGatewayError):
    """A credit file's priced model spend has reached the
    deployment's per-file cap; further live-profile calls for
    that file are refused *before* any transport is touched. The
    refusal is recorded — a budget event in the ledger and an
    audit event on the file. The offline profile is never capped,
    and continuing offline is the caller's recorded choice, never
    the gateway switching on its own."""

    def __init__(
        self,
        credit_file_id: str,
        profile_id: str,
        cap_usd: str,
        spent_usd: str,
    ) -> None:
        super().__init__(
            f"model budget for credit file {credit_file_id} is "
            f"exhausted: priced spend ${spent_usd} has reached the "
            f"per-file cap of ${cap_usd}; live profile "
            f"{profile_id!r} is refused before transport. The "
            "offline profile remains available"
        )
        self.credit_file_id = credit_file_id
        self.profile_id = profile_id
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd
