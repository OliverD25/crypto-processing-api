/**
 * What the client throws, and what each one means for your next line of code.
 *
 * The split is by what you can do about it, not by status code:
 *
 * - `ValidationError`, `NotFoundError`, `AuthenticationError`,
 *   `PermissionDeniedError` and `UpstreamRefusedError` mean the request is
 *   dead. Sending it again sends the same wrong request.
 * - `ConflictError` and `ServiceUnavailableError` mean try again with the
 *   **same** `Idempotency-Key`. The client already does that for you, up to
 *   `RetryPolicy.attempts`; these reach you when it has run out.
 */

/** Base class. Catching this catches everything the client throws. */
export class CryptoProcessingError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = new.target.name;
  }
}

/** A delivered webhook did not verify. Answer 401 and do nothing else. */
export class WebhookVerificationError extends CryptoProcessingError {}

/**
 * A delivered event has a `type` this client's version does not know.
 *
 * New event types arrive in any minor release, so this is not an error in the
 * sense of something being wrong — it is the documented "skip it" case.
 * Acknowledge the delivery with a 2xx anyway, or the server will retry an
 * event you are never going to handle.
 */
export class UnknownEventTypeError extends CryptoProcessingError {
  readonly eventType: string;

  constructor(eventType: string) {
    super(
      `unknown event type ${JSON.stringify(eventType)}; acknowledge it and skip it rather than failing`,
    );
    this.eventType = eventType;
  }
}

/** The request never got an answer: DNS, TLS, connection, timeout.
 *
 * A retry is safe on any endpoint, because the mutating ones carry an
 * `Idempotency-Key`. That is the whole reason the key exists.
 */
export class TransportError extends CryptoProcessingError {}

export interface ApiErrorInit {
  /** Set only where the API defines one — today, the pooled-address code. */
  code?: string | undefined;
  /** Seconds to wait, from `Retry-After`, when the server sent one. */
  retryAfter?: number | undefined;
  /** FastAPI's per-field list, when the body did not fit the request model. */
  fieldErrors?: ReadonlyArray<Record<string, unknown>>;
  /** Whatever the server actually sent, for logging something unmodelled. */
  body?: unknown;
}

/** The server answered, and the answer was a refusal. */
export class ApiError extends CryptoProcessingError {
  readonly statusCode: number;
  readonly code: string | undefined;
  readonly retryAfter: number | undefined;
  readonly fieldErrors: ReadonlyArray<Record<string, unknown>>;
  readonly body: unknown;

  constructor(statusCode: number, message: string, init: ApiErrorInit = {}) {
    super(`${statusCode}: ${message}`);
    this.statusCode = statusCode;
    this.code = init.code;
    this.retryAfter = init.retryAfter;
    this.fieldErrors = init.fieldErrors ?? [];
    this.body = init.body;
  }
}

/** 400. In practice: the `Idempotency-Key` header was missing or unusable. */
export class BadRequestError extends ApiError {}

/** 401. Missing, malformed, revoked or expired API key. */
export class AuthenticationError extends ApiError {}

/** 403. A valid key with the wrong scope — a `readwrite` key on an admin route. */
export class PermissionDeniedError extends ApiError {}

/** 404. No such deposit, withdrawal, user or asset. */
export class NotFoundError extends ApiError {}

/**
 * 409. Either a request with this key is still in flight, or the transition is
 * illegal. The two are told apart by `retryAfter`: the in-flight case carries
 * one and is retried automatically, the illegal-transition case does not.
 */
export class ConflictError extends ApiError {}

/**
 * 422. A business rule refused it, the body did not fit, or a key was reused.
 * `fieldErrors` is populated for the body-shape case and empty for the others.
 */
export class ValidationError extends ApiError {}

/** 502. BTCPay answered and said no. The intent is dead; start a new one. */
export class UpstreamRefusedError extends ApiError {}

/** 503. Temporary. Retry with the same `Idempotency-Key`, honouring `retryAfter`. */
export class ServiceUnavailableError extends ApiError {}

/** Any other 5xx. Nothing the API documents; treat it as temporary. */
export class ServerError extends ApiError {}

const BY_STATUS: Record<number, new (status: number, message: string, init?: ApiErrorInit) => ApiError> = {
  400: BadRequestError,
  401: AuthenticationError,
  403: PermissionDeniedError,
  404: NotFoundError,
  409: ConflictError,
  422: ValidationError,
  502: UpstreamRefusedError,
  503: ServiceUnavailableError,
};

/** Status code to class. Any other 5xx is a `ServerError`, any other 4xx an `ApiError`. */
export function errorClass(
  statusCode: number,
): new (status: number, message: string, init?: ApiErrorInit) => ApiError {
  return BY_STATUS[statusCode] ?? (statusCode >= 500 ? ServerError : ApiError);
}
