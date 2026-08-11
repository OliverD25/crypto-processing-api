"""What the client raises, and what each one means for your next line of code.

The generated core returns error bodies as ordinary values, which makes a
missing check look exactly like a success. Everything the facade returns is a
success; everything else is one of these.

The split is by what you can do about it, not by status code:

- `ValidationError`, `NotFoundError`, `AuthenticationError`,
  `PermissionDeniedError` and `UpstreamRefusedError` mean the request is dead.
  Sending it again sends the same wrong request.
- `ConflictError` and `ServiceUnavailableError` mean try again with the **same**
  `Idempotency-Key`. The client already does that for you, up to
  `RetryPolicy.attempts`; these reach you when it has run out.
"""

from __future__ import annotations

from typing import Any


class CryptoProcessingError(Exception):
    """Base class. Catching this catches everything the client raises."""


class WebhookVerificationError(CryptoProcessingError):
    """A delivered webhook did not verify. Answer 401 and do nothing else."""


class UnknownEventTypeError(CryptoProcessingError):
    """A delivered event has a `type` this client's version does not know.

    New event types arrive in any minor release, so this is not an error in
    the sense of something being wrong — it is the documented "skip it" case.
    Acknowledge the delivery with a 2xx anyway, or the server will retry an
    event you are never going to handle.
    """

    def __init__(self, event_type: str) -> None:
        super().__init__(
            f"unknown event type {event_type!r}; acknowledge it and skip it rather than failing"
        )
        self.event_type = event_type


class APIError(CryptoProcessingError):
    """The server answered, and the answer was a refusal."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        code: str | None = None,
        retry_after: float | None = None,
        field_errors: list[dict[str, Any]] | None = None,
        body: bytes = b"",
    ) -> None:
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code
        self.message = message
        #: Set only where the API defines one. Today that is the pooled-address
        #: `DEPOSIT_TEMPORARILY_UNAVAILABLE`; `message` is never a stable
        #: machine identifier and must not be matched on.
        self.code = code
        #: Seconds to wait, from `Retry-After`, when the server sent one.
        self.retry_after = retry_after
        #: FastAPI's per-field list, when the body did not fit the request model.
        self.field_errors = field_errors or []
        #: The raw response body, for logging something the server did not model.
        self.body = body


class BadRequestError(APIError):
    """400. In practice: the `Idempotency-Key` header was missing or unusable."""


class AuthenticationError(APIError):
    """401. Missing, malformed, revoked or expired API key."""


class PermissionDeniedError(APIError):
    """403. A valid key with the wrong scope — a `readwrite` key on an admin route."""


class NotFoundError(APIError):
    """404. No such deposit, withdrawal, user or asset."""


class ConflictError(APIError):
    """409. Either a request with this key is still in flight, or the transition is illegal.

    The two are told apart by `retry_after`: the in-flight case carries one and
    is retried automatically, the illegal-transition case does not.
    """


class ValidationError(APIError):
    """422. A business rule refused it, the body did not fit, or a key was reused.

    `field_errors` is populated for the body-shape case and empty for the other
    two, where `message` carries the reason.
    """


class UpstreamRefusedError(APIError):
    """502. BTCPay answered and said no. The intent is dead; start a new one."""


class ServiceUnavailableError(APIError):
    """503. Temporary. Retry with the same `Idempotency-Key`, honouring `retry_after`."""


class ServerError(APIError):
    """Any other 5xx. Nothing the API documents; treat it as temporary."""


class TransportError(CryptoProcessingError):
    """The request never got an answer: DNS, TLS, connection, timeout.

    A retry is safe on any endpoint, because the mutating ones carry an
    `Idempotency-Key`. That is the whole reason the key exists.
    """


#: Status code to class. Anything else 4xx becomes `APIError`, anything else
#: 5xx becomes `ServerError`.
BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    502: UpstreamRefusedError,
    503: ServiceUnavailableError,
}


def error_class(status_code: int) -> type[APIError]:
    if status_code in BY_STATUS:
        return BY_STATUS[status_code]
    return ServerError if status_code >= 500 else APIError
