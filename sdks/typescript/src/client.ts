/**
 * The client you call.
 *
 * Everything under `src/generated` is produced from the committed OpenAPI
 * document and is regenerated whenever a route changes. This module is the
 * part codegen cannot write, and it is deliberately small:
 *
 * - **Idempotency.** Every mutating call carries an `Idempotency-Key`. The
 *   server requires one and answers 400 without it, so minting a UUID by
 *   default is strictly better than making every caller remember. The key is
 *   minted once per logical call and **reused on every retry of that call**,
 *   which is the rule `docs/integrating.md` puts in bold: a retry with a new
 *   key is a second deposit, not a retry.
 * - **Retries.** 503 always, and 409 only when the server sent a
 *   `Retry-After` — that is how the "your own request is still in flight" 409
 *   is told apart from the "this state transition is illegal" 409, which no
 *   amount of waiting fixes. Transport failures are retried too, which is safe
 *   precisely because the key is pinned.
 * - **Errors.** A refusal throws. The generated core returns error bodies as
 *   ordinary values, and a caller who forgets one check gets a
 *   `DepositResponse` variable holding an `ErrorResponse`.
 *
 * Amounts are strings everywhere, in both directions, and this layer never
 * converts them. `expected_amount` and `amount` are integer smallest units
 * (`"50000000"`); everything the server returns is a decimal string
 * (`"0.50000000"`). `Number()` on either is how a satoshi goes missing — 21
 * million BTC in satoshis is past JavaScript's safe integer range.
 */

import * as errors from './errors.js';
import { type Client, createClient, createConfig } from './generated/client/index.js';
import {
  adminApproveWithdrawal,
  adminListEvents,
  adminMarkBroadcast,
  adminReconciliation,
  adminRedeliverEvent,
  adminRejectWithdrawal,
  adminReleaseWithdrawal,
  adminResolveDeposit,
  adminReviewQueue,
  adminWalletAlerts,
  adminWithdrawalQueue,
  createDeposit,
  createWithdrawal,
  getAddressHistory,
  getDeposit,
  getUserBalances,
  getUserTransactions,
  getWithdrawal,
  healthz,
  listAssets,
  listUserDeposits,
  listUserWithdrawals,
  readyz,
} from './generated/sdk.gen.js';
import type {
  AddressHistoryResponse,
  AdminDepositQueueResponse,
  AdminResolveDepositResponse2,
  AdminWithdrawalListResponse,
  ApproveWithdrawalRequest,
  AssetsResponse,
  BalancesResponse,
  CreateDepositRequest,
  CreateWithdrawalRequest,
  DepositListResponse,
  DepositResponse,
  HealthResponse,
  MarkBroadcastRequest,
  OutboundEventsResponse,
  ReadyResponse,
  ReconciliationResponse,
  RedeliverEventResponse,
  RejectWithdrawalRequest,
  ReleaseWithdrawalRequest,
  ResolveDepositRequest,
  TransactionsResponse,
  WalletAlertsResponse,
  WithdrawalCreatedResponse,
  WithdrawalListResponse,
  WithdrawalResponse,
} from './generated/types.gen.js';

/** How hard the client tries before it gives up and throws. */
export interface RetryPolicy {
  /** Counts the first try. `1` disables retrying without disabling the key. */
  attempts: number;
  /** Used when the server did not send a `Retry-After`, doubled each time. */
  backoffSeconds: number;
  /** A server may ask for a longer wait than a request handler can afford. */
  maxDelaySeconds: number;
}

export const DEFAULT_RETRY: RetryPolicy = {
  attempts: 3,
  backoffSeconds: 0.5,
  maxDelaySeconds: 60,
};

export interface CryptoProcessingClientOptions {
  /** e.g. `https://pay.example.com`. A trailing slash is fine. */
  baseUrl: string;
  /** `cpk_live_…`. Sent as `Authorization: Bearer …`. */
  apiKey: string;
  /** Per attempt, not per logical call. Default 30 seconds. */
  timeoutMs?: number;
  retry?: Partial<RetryPolicy>;
  /** Extra headers on every request — a `User-Agent`, a trace id. */
  headers?: Record<string, string>;
  /** Your own fetch: a proxy agent, a test double, an instrumented wrapper. */
  fetch?: typeof fetch;
  /** Waits between retries. Exposed so a test does not have to wait. */
  sleep?: (seconds: number) => Promise<void>;
}

/** Statuses the retry loop waits on rather than throwing. 409 is conditional. */
const RETRYABLE = new Set([409, 503]);

interface Envelope<TData> {
  data?: TData | undefined;
  error?: unknown;
  request?: Request | undefined;
  response?: Response | undefined;
}

type Invoke<TData> = (extra: {
  client: Client;
  signal: AbortSignal | undefined;
}) => Promise<Envelope<TData>>;

function defaultSleep(seconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, seconds * 1000));
}

/** `Retry-After` is either a number of seconds or an HTTP date. */
export function retryAfterSeconds(headers: Headers | undefined): number | undefined {
  const raw = headers?.get('retry-after');
  if (!raw) return undefined;
  if (/^\d+(\.\d+)?$/.test(raw.trim())) return Math.max(0, Number(raw));
  const target = Date.parse(raw);
  if (Number.isNaN(target)) return undefined;
  return Math.max(0, (target - Date.now()) / 1000);
}

/** Pull (message, code, field errors) out of whichever error shape arrived. */
function describe(body: unknown): {
  message: string;
  code: string | undefined;
  fieldErrors: ReadonlyArray<Record<string, unknown>>;
} {
  if (typeof body === 'string') {
    return { message: body.trim().slice(0, 500) || 'the server gave no reason', code: undefined, fieldErrors: [] };
  }
  if (typeof body !== 'object' || body === null) {
    return { message: 'the server gave no reason', code: undefined, fieldErrors: [] };
  }
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === 'string') {
    return { message: detail, code: undefined, fieldErrors: [] };
  }
  if (Array.isArray(detail)) {
    return {
      message: 'the request body was refused; see fieldErrors',
      code: undefined,
      fieldErrors: detail as ReadonlyArray<Record<string, unknown>>,
    };
  }
  if (typeof detail === 'object' && detail !== null) {
    // The one structured detail: pooled-address exhaustion, which carries a
    // code a caller is meant to branch on.
    const structured = detail as { code?: unknown; message?: unknown };
    return {
      message: typeof structured.message === 'string' ? structured.message : 'the request was refused',
      code: typeof structured.code === 'string' ? structured.code : undefined,
      fieldErrors: [],
    };
  }
  return { message: 'the request was refused', code: undefined, fieldErrors: [] };
}

function newIdempotencyKey(): string {
  return globalThis.crypto.randomUUID();
}

/**
 * A configured connection to one crypto-processing-api deployment.
 *
 * ```ts
 * const client = new CryptoProcessingClient({
 *   baseUrl: 'https://pay.example.com',
 *   apiKey: 'cpk_live_...',
 * });
 * const deposit = await client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' });
 * ```
 *
 * One long-lived client is the intended shape. Request bodies use the API's
 * own field names rather than camelCase equivalents, so there is no mapping
 * layer between this and the generated types that could drift.
 */
export class CryptoProcessingClient {
  readonly retry: RetryPolicy;
  readonly #client: Client;
  readonly #timeoutMs: number;
  readonly #sleep: (seconds: number) => Promise<void>;

  constructor(options: CryptoProcessingClientOptions) {
    if (!options.baseUrl) throw new TypeError('baseUrl is required, e.g. https://pay.example.com');
    if (!options.apiKey) throw new TypeError('apiKey is required; the API refuses unauthenticated calls');
    this.retry = { ...DEFAULT_RETRY, ...options.retry };
    this.#timeoutMs = options.timeoutMs ?? 30_000;
    this.#sleep = options.sleep ?? defaultSleep;
    this.#client = createClient(
      createConfig({
        baseUrl: options.baseUrl.replace(/\/+$/, ''),
        auth: () => options.apiKey,
        headers: options.headers ?? {},
        ...(options.fetch ? { fetch: options.fetch } : {}),
        throwOnError: false,
      }),
    );
  }

  /** The underlying generated client, for an endpoint this facade has not wrapped. */
  get generatedClient(): Client {
    return this.#client;
  }

  // -- deposits -------------------------------------------------------------

  /**
   * Create a deposit and its invoice. `POST /v1/deposits`.
   *
   * `expected_amount` is an integer number of the asset's smallest units, as a
   * string. It is display-only for BTC and load-bearing for USDT, where a
   * payment far from it goes to an operator instead of being credited.
   *
   * Pass `idempotencyKey` when your own system already has an id for this
   * logical operation. Leave it out and a UUID is minted for you and reused
   * across every retry of this call.
   */
  createDeposit(
    body: CreateDepositRequest,
    options: { idempotencyKey?: string } = {},
  ): Promise<DepositResponse> {
    const key = options.idempotencyKey ?? newIdempotencyKey();
    return this.#send<DepositResponse>((extra) =>
      createDeposit({ ...extra, body, headers: { 'Idempotency-Key': key } }),
    );
  }

  /**
   * Read one deposit. `GET /v1/deposits/{deposit_id}`.
   *
   * This is the call that tells you what is true. A webhook only tells you
   * something changed.
   */
  getDeposit(depositId: string): Promise<DepositResponse> {
    return this.#send<DepositResponse>((extra) =>
      getDeposit({ ...extra, path: { deposit_id: depositId } }),
    );
  }

  /** One user's deposits, newest first. `GET /v1/users/{id}/deposits`. */
  listUserDeposits(
    externalUserId: string,
    query: { limit?: number; cursor?: string } = {},
  ): Promise<DepositListResponse> {
    return this.#send<DepositListResponse>((extra) =>
      listUserDeposits({ ...extra, path: { external_user_id: externalUserId }, query }),
    );
  }

  /** Which deposits have held this deposit's address. Pooled assets only. */
  getAddressHistory(depositId: string): Promise<AddressHistoryResponse> {
    return this.#send<AddressHistoryResponse>((extra) =>
      getAddressHistory({ ...extra, path: { deposit_id: depositId } }),
    );
  }

  // -- withdrawals ----------------------------------------------------------

  /**
   * Request a withdrawal. `POST /v1/withdrawals`.
   *
   * `amount` is the **gross** amount in integer smallest units, as a string.
   * The fee comes out of it, so the user receives less than this.
   *
   * The hold on the user's balance is placed before you get an answer, so a
   * connection that drops mid-call may still have moved money. Retrying with
   * the same key is what makes that safe, and this method does it for you.
   */
  createWithdrawal(
    body: CreateWithdrawalRequest,
    options: { idempotencyKey?: string } = {},
  ): Promise<WithdrawalCreatedResponse> {
    const key = options.idempotencyKey ?? newIdempotencyKey();
    return this.#send<WithdrawalCreatedResponse>((extra) =>
      createWithdrawal({ ...extra, body, headers: { 'Idempotency-Key': key } }),
    );
  }

  /** Read one withdrawal. `GET /v1/withdrawals/{withdrawal_id}`. */
  getWithdrawal(withdrawalId: string): Promise<WithdrawalResponse> {
    return this.#send<WithdrawalResponse>((extra) =>
      getWithdrawal({ ...extra, path: { withdrawal_id: withdrawalId } }),
    );
  }

  /** One user's withdrawals, newest first. `GET /v1/users/{id}/withdrawals`. */
  listUserWithdrawals(
    externalUserId: string,
    query: { limit?: number; cursor?: string } = {},
  ): Promise<WithdrawalListResponse> {
    return this.#send<WithdrawalListResponse>((extra) =>
      listUserWithdrawals({ ...extra, path: { external_user_id: externalUserId }, query }),
    );
  }

  // -- balances and history -------------------------------------------------

  /**
   * Every asset balance for one user. `GET /v1/users/{id}/balances`.
   *
   * The API is the source of truth for these. Storing your own copy and
   * reconciling it later is the thing this service exists to spare you.
   */
  getUserBalances(externalUserId: string): Promise<BalancesResponse> {
    return this.#send<BalancesResponse>((extra) =>
      getUserBalances({ ...extra, path: { external_user_id: externalUserId } }),
    );
  }

  /** One user's ledger movements. `GET /v1/users/{id}/transactions`. */
  getUserTransactions(
    externalUserId: string,
    query: { asset?: string; limit?: number; cursor?: number } = {},
  ): Promise<TransactionsResponse> {
    return this.#send<TransactionsResponse>((extra) =>
      getUserTransactions({ ...extra, path: { external_user_id: externalUserId }, query }),
    );
  }

  /** Which assets this deployment has enabled. `GET /v1/assets`. */
  listAssets(): Promise<AssetsResponse> {
    return this.#send<AssetsResponse>((extra) => listAssets({ ...extra }));
  }

  // -- health ---------------------------------------------------------------

  /** This process and its database. `GET /healthz`. No API key needed. */
  healthz(): Promise<HealthResponse> {
    return this.#send<HealthResponse>((extra) => healthz({ ...extra }));
  }

  /**
   * Component readiness. `GET /readyz`.
   *
   * A degraded deployment answers 503 with the same body naming which
   * component is unhappy, and that is a normal answer here rather than an
   * exception — read `status` and `components`.
   */
  readyz(): Promise<ReadyResponse> {
    return this.#send<ReadyResponse>((extra) => readyz({ ...extra }), [503]);
  }

  // -- operator endpoints ---------------------------------------------------

  /** Payments waiting for a human. `GET /v1/admin/deposits/review`. Needs an admin key. */
  adminReviewQueue(query: { limit?: number } = {}): Promise<AdminDepositQueueResponse> {
    return this.#send<AdminDepositQueueResponse>((extra) => adminReviewQueue({ ...extra, query }));
  }

  /**
   * Credit or dismiss a reviewed payment. `POST /v1/admin/deposits/{id}/resolve`.
   *
   * There is no amount to pass: the server asks BTCPay what the payment was
   * worth. An operator confirms attribution and nothing else.
   */
  adminResolveDeposit(
    depositId: string,
    body: ResolveDepositRequest,
  ): Promise<AdminResolveDepositResponse2> {
    return this.#send<AdminResolveDepositResponse2>((extra) =>
      adminResolveDeposit({ ...extra, path: { deposit_id: depositId }, body }),
    );
  }

  /** The approval queue. `GET /v1/admin/withdrawals?status=pending_approval`. */
  adminWithdrawalQueue(
    query: { status?: string; limit?: number } = {},
  ): Promise<AdminWithdrawalListResponse> {
    return this.#send<AdminWithdrawalListResponse>((extra) =>
      adminWithdrawalQueue({ ...extra, query }),
    );
  }

  /** Let a held withdrawal proceed. `POST /v1/admin/withdrawals/{id}/approve`. */
  adminApproveWithdrawal(
    withdrawalId: string,
    body: ApproveWithdrawalRequest = {},
  ): Promise<WithdrawalResponse> {
    return this.#send<WithdrawalResponse>((extra) =>
      adminApproveWithdrawal({ ...extra, path: { withdrawal_id: withdrawalId }, body }),
    );
  }

  /** Refuse a held withdrawal and return the money. `POST .../reject`. */
  adminRejectWithdrawal(
    withdrawalId: string,
    body: RejectWithdrawalRequest = {},
  ): Promise<WithdrawalResponse> {
    return this.#send<WithdrawalResponse>((extra) =>
      adminRejectWithdrawal({ ...extra, path: { withdrawal_id: withdrawalId }, body }),
    );
  }

  /** Report the transaction an operator sent by hand. `POST .../mark-broadcast`. */
  adminMarkBroadcast(
    withdrawalId: string,
    body: MarkBroadcastRequest,
  ): Promise<WithdrawalResponse> {
    return this.#send<WithdrawalResponse>((extra) =>
      adminMarkBroadcast({ ...extra, path: { withdrawal_id: withdrawalId }, body }),
    );
  }

  /**
   * Return a hold after a payout may exist. `POST .../release`.
   *
   * `attestation` is a human stating that they looked and the coins are not
   * arriving. It is stored on the withdrawal, so the decision has an author.
   */
  adminReleaseWithdrawal(
    withdrawalId: string,
    body: ReleaseWithdrawalRequest,
  ): Promise<WithdrawalResponse> {
    return this.#send<WithdrawalResponse>((extra) =>
      adminReleaseWithdrawal({ ...extra, path: { withdrawal_id: withdrawalId }, body }),
    );
  }

  /** The latest sweep: what the books say against what the chain says. */
  adminReconciliation(): Promise<ReconciliationResponse> {
    return this.#send<ReconciliationResponse>((extra) => adminReconciliation({ ...extra }));
  }

  /** Coins in the hot wallet that match no known deposit. BTC only. */
  adminWalletAlerts(query: { limit?: number } = {}): Promise<WalletAlertsResponse> {
    return this.#send<WalletAlertsResponse>((extra) => adminWalletAlerts({ ...extra, query }));
  }

  /** The outbound event queue, including anything dead-lettered. */
  adminListEvents(
    query: { status?: string; limit?: number } = {},
  ): Promise<OutboundEventsResponse> {
    return this.#send<OutboundEventsResponse>((extra) => adminListEvents({ ...extra, query }));
  }

  /** Put a dead-lettered event back in the queue. `POST /v1/admin/events/{id}/redeliver`. */
  adminRedeliverEvent(eventId: string): Promise<RedeliverEventResponse> {
    return this.#send<RedeliverEventResponse>((extra) =>
      adminRedeliverEvent({ ...extra, path: { event_id: eventId } }),
    );
  }

  // -- the machinery --------------------------------------------------------

  /** Seconds to wait before the next attempt, or undefined to give up. */
  #delayFor(error: errors.ApiError | undefined, attempt: number): number | undefined {
    if (attempt + 1 >= this.retry.attempts) return undefined;
    const backoff = Math.min(
      this.retry.backoffSeconds * 2 ** attempt,
      this.retry.maxDelaySeconds,
    );
    if (error === undefined) return backoff;
    if (!RETRYABLE.has(error.statusCode)) return undefined;
    if (error.statusCode === 409 && error.retryAfter === undefined) {
      // A 409 with no Retry-After is an illegal state transition — the
      // withdrawal is not in the status this call needs. Waiting will never
      // change that, so throw it now rather than in three seconds.
      return undefined;
    }
    if (error.retryAfter !== undefined) {
      return Math.min(error.retryAfter, this.retry.maxDelaySeconds);
    }
    return backoff;
  }

  /** One logical call, retried in place with the key it started with. */
  async #send<TData>(invoke: Invoke<TData>, allowStatuses: readonly number[] = []): Promise<TData> {
    for (let attempt = 0; ; attempt += 1) {
      const result = await invoke({
        client: this.#client,
        signal: AbortSignal.timeout(this.#timeoutMs),
      });

      if (result.response === undefined) {
        // No response at all: DNS, TLS, connection reset, or the timeout above.
        const delay = this.#delayFor(undefined, attempt);
        if (delay === undefined) {
          throw new errors.TransportError(
            `the request never got an answer: ${String(
              result.error instanceof Error ? result.error.message : result.error,
            )}`,
          );
        }
        await this.#sleep(delay);
        continue;
      }

      const status = result.response.status;
      if (status < 400 || allowStatuses.includes(status)) {
        return (result.data ?? result.error) as TData;
      }

      const { message, code, fieldErrors } = describe(result.error);
      const failure = new (errors.errorClass(status))(status, message, {
        code,
        retryAfter: retryAfterSeconds(result.response.headers),
        fieldErrors,
        body: result.error,
      });
      const delay = this.#delayFor(failure, attempt);
      if (delay === undefined) throw failure;
      await this.#sleep(delay);
    }
  }
}
