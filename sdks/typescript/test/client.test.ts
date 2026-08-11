/**
 * The facade, against a fetch that answers whatever a test needs.
 *
 * Nothing here talks to a server. What is being checked is the behaviour
 * codegen cannot produce: that a key is minted, that a retry reuses it, that a
 * refusal throws something a caller can branch on, and that a proxy's HTML
 * error page does not arrive as an unhandled parse error.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import {
  AuthenticationError,
  BadRequestError,
  ConflictError,
  CryptoProcessingClient,
  NotFoundError,
  PermissionDeniedError,
  ServerError,
  ServiceUnavailableError,
  TransportError,
  UpstreamRefusedError,
  ValidationError,
  type RetryPolicy,
} from '../src/index.js';

const BASE_URL = 'https://pay.example.test';
const API_KEY = 'cpk_live_not_a_real_key';

const DEPOSIT_BODY = {
  deposit_id: '019feb96-7e52-771a-a8cb-a86dccc87339',
  external_user_id: 'user-42',
  asset: 'BTC',
  status: 'pending',
  address: 'bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yq',
  checkout_link: 'https://btcpay.example.test/i/JRr',
  expires_at: '2026-08-10T13:15:13+00:00',
  address_reserved_until: null,
  amount_expected: '0.50000000',
  amount_credited: '0.00000000',
  created_at: '2026-08-10T12:15:13+00:00',
  payments: [],
};

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

/** Every request the client made, in order, already read. */
let seen: Array<{ url: string; method: string; headers: Headers; body: string }>;
/** What the retry loop would have waited, instead of waiting. */
let waited: number[];

beforeEach(() => {
  seen = [];
  waited = [];
});

function build(
  answer: (attempt: number) => Response | Promise<Response>,
  retry: Partial<RetryPolicy> = { attempts: 3, backoffSeconds: 0 },
): CryptoProcessingClient {
  return new CryptoProcessingClient({
    baseUrl: BASE_URL,
    apiKey: API_KEY,
    retry,
    sleep: async (seconds) => {
      waited.push(seconds);
    },
    fetch: async (input) => {
      const request = input as Request;
      seen.push({
        url: request.url,
        method: request.method,
        headers: request.headers,
        body: await request.clone().text(),
      });
      return answer(seen.length - 1);
    },
  });
}

// -- configuration ----------------------------------------------------------

describe('configuration', () => {
  it('sends the api key as a bearer header', async () => {
    const client = build(() => jsonResponse(201, DEPOSIT_BODY));
    await client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' });
    expect(seen[0]?.headers.get('authorization')).toBe(`Bearer ${API_KEY}`);
    expect(seen[0]?.url).toBe(`${BASE_URL}/v1/deposits`);
  });

  it('does not double up a trailing slash on the base url', async () => {
    const client = new CryptoProcessingClient({
      baseUrl: `${BASE_URL}/`,
      apiKey: API_KEY,
      fetch: async (input) => {
        seen.push({
          url: (input as Request).url,
          method: 'GET',
          headers: new Headers(),
          body: '',
        });
        return jsonResponse(200, { assets: [] });
      },
    });
    await client.listAssets();
    expect(seen[0]?.url).toBe(`${BASE_URL}/v1/assets`);
  });

  it('fails before any request when the api key is missing', () => {
    expect(() => new CryptoProcessingClient({ baseUrl: BASE_URL, apiKey: '' })).toThrow(TypeError);
  });
});

// -- idempotency ------------------------------------------------------------

describe('idempotency', () => {
  it('mints a key when the caller does not supply one', async () => {
    // The server answers 400 without one, so the default cannot be 'absent'.
    const client = build(() => jsonResponse(201, DEPOSIT_BODY));
    await client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' });
    expect(seen[0]?.headers.get('idempotency-key')?.length).toBeGreaterThanOrEqual(32);
  });

  it('uses an explicit key unchanged', async () => {
    const client = build(() => jsonResponse(201, DEPOSIT_BODY));
    await client.createDeposit(
      { external_user_id: 'user-42', asset: 'BTC' },
      { idempotencyKey: 'order-1234' },
    );
    expect(seen[0]?.headers.get('idempotency-key')).toBe('order-1234');
  });

  it('gives two calls two different keys', async () => {
    // One key per logical operation. A shared key would make the second a replay.
    const client = build(() => jsonResponse(201, DEPOSIT_BODY));
    await client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' });
    await client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' });
    expect(seen[0]?.headers.get('idempotency-key')).not.toBe(
      seen[1]?.headers.get('idempotency-key'),
    );
  });

  it('reuses the key it started with on a retry', async () => {
    // The rule the docs put in bold: a new key is a second deposit, not a retry.
    const client = build((attempt) =>
      attempt === 0
        ? jsonResponse(503, { detail: 'BTCPay is unreachable' }, { 'retry-after': '2' })
        : jsonResponse(201, DEPOSIT_BODY),
    );
    const deposit = await client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' });
    expect(seen).toHaveLength(2);
    expect(seen[0]?.headers.get('idempotency-key')).toBe(seen[1]?.headers.get('idempotency-key'));
    expect(deposit.status).toBe('pending');
  });

  it('sends byte-identical bodies on a retry', async () => {
    // The request hash is over the exact bytes; a re-serialization reads as 422.
    const client = build((attempt) =>
      attempt === 0
        ? jsonResponse(503, { detail: 'unreachable' }, { 'retry-after': '0' })
        : jsonResponse(201, DEPOSIT_BODY),
    );
    await client.createDeposit({
      external_user_id: 'user-42',
      asset: 'BTC',
      expected_amount: '50000000',
    });
    expect(seen[0]?.body).toBe(seen[1]?.body);
  });
});

// -- retrying ---------------------------------------------------------------

describe('retrying', () => {
  it('honours Retry-After', async () => {
    const client = build(() =>
      jsonResponse(503, { detail: 'unreachable' }, { 'retry-after': '7' }),
    );
    await expect(
      client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' }),
    ).rejects.toBeInstanceOf(ServiceUnavailableError);
    expect(waited).toEqual([7, 7]);
  });

  it('caps a Retry-After the caller cannot afford to honour', async () => {
    const client = build(
      () => jsonResponse(503, { detail: 'unreachable' }, { 'retry-after': '3600' }),
      { attempts: 2, maxDelaySeconds: 5 },
    );
    await expect(client.listAssets()).rejects.toBeInstanceOf(ServiceUnavailableError);
    expect(waited).toEqual([5]);
  });

  it('understands a Retry-After given as an HTTP date', async () => {
    // RFC 9110 allows a date. A client that only parses integers waits zero.
    const client = build(
      () =>
        jsonResponse(
          503,
          { detail: 'unreachable' },
          { 'retry-after': 'Wed, 21 Oct 2099 07:28:00 GMT' },
        ),
      { attempts: 2, maxDelaySeconds: 30 },
    );
    await expect(client.listAssets()).rejects.toBeInstanceOf(ServiceUnavailableError);
    expect(waited).toEqual([30]);
  });

  it('retries an in-flight 409', async () => {
    // Retry-After present means 'your own earlier attempt is still running'.
    const client = build((attempt) =>
      attempt === 0
        ? jsonResponse(409, { detail: 'still in progress' }, { 'retry-after': '1' })
        : jsonResponse(201, DEPOSIT_BODY),
    );
    await client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' });
    expect(seen).toHaveLength(2);
    expect(seen[0]?.headers.get('idempotency-key')).toBe(seen[1]?.headers.get('idempotency-key'));
  });

  it('throws an illegal-transition 409 at once', async () => {
    // No Retry-After means waiting cannot help. Three tries would just be slower.
    const client = build(() =>
      jsonResponse(409, { detail: 'withdrawal is not pending_approval' }),
    );
    await expect(
      client.adminApproveWithdrawal('019feb97-1c04-7b2e-9f3a-2d5c7e8b1a06'),
    ).rejects.toBeInstanceOf(ConflictError);
    expect(seen).toHaveLength(1);
  });

  it('never retries a 502', async () => {
    // BTCPay answered and refused. The intent is dead; retrying wastes time.
    const client = build(() => jsonResponse(502, { detail: 'BTCPay rejected the invoice' }));
    await expect(
      client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' }),
    ).rejects.toBeInstanceOf(UpstreamRefusedError);
    expect(seen).toHaveLength(1);
  });

  it('disables retrying without disabling the key when attempts is one', async () => {
    const client = build(
      () => jsonResponse(503, { detail: 'unreachable' }, { 'retry-after': '1' }),
      { attempts: 1 },
    );
    await expect(
      client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' }),
    ).rejects.toBeInstanceOf(ServiceUnavailableError);
    expect(seen).toHaveLength(1);
    expect(seen[0]?.headers.get('idempotency-key')).toBeTruthy();
  });

  it('retries a dropped connection with the same key', async () => {
    // Safe only because the key is pinned. That is what the key is for.
    const client = build((attempt) => {
      if (attempt === 0) throw new TypeError('fetch failed');
      return jsonResponse(201, DEPOSIT_BODY);
    });
    await client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' });
    expect(seen[0]?.headers.get('idempotency-key')).toBe(seen[1]?.headers.get('idempotency-key'));
  });

  it('gives up on a connection that never comes back', async () => {
    const client = build(() => {
      throw new TypeError('fetch failed');
    });
    await expect(client.getUserBalances('user-42')).rejects.toBeInstanceOf(TransportError);
  });
});

// -- error mapping ----------------------------------------------------------

describe('error mapping', () => {
  const cases: Array<[number, new (...args: never[]) => Error]> = [
    [400, BadRequestError],
    [401, AuthenticationError],
    [403, PermissionDeniedError],
    [404, NotFoundError],
    [502, UpstreamRefusedError],
    [500, ServerError],
  ];

  for (const [status, expected] of cases) {
    it(`turns ${status} into ${expected.name}`, async () => {
      const client = build(() => jsonResponse(status, { detail: 'no' }));
      await expect(
        client.getDeposit('019feb96-7e52-771a-a8cb-a86dccc87339'),
      ).rejects.toBeInstanceOf(expected);
    });
  }

  it('carries the reason of a business-rule 422', async () => {
    const client = build(() =>
      jsonResponse(422, { detail: 'amount is below the dust threshold' }),
    );
    await expect(
      client.createWithdrawal({
        external_user_id: 'user-42',
        asset: 'BTC',
        amount: '1',
        destination_address: 'bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yq',
      }),
    ).rejects.toMatchObject({ message: expect.stringContaining('dust'), fieldErrors: [] });
  });

  it('carries the field errors of a body-shape 422', async () => {
    const client = build(() =>
      jsonResponse(422, {
        detail: [{ loc: ['body', 'amount'], msg: 'field required', type: 'missing' }],
      }),
    );
    await expect(
      client.createWithdrawal({
        external_user_id: 'user-42',
        asset: 'BTC',
        amount: '',
        destination_address: 'x',
      }),
    ).rejects.toSatisfy(
      (error: unknown) =>
        error instanceof ValidationError &&
        JSON.stringify(error.fieldErrors[0]?.['loc']) === '["body","amount"]',
    );
  });

  it('keeps the machine-readable code of pool exhaustion', async () => {
    // The one error a caller is meant to branch on rather than log.
    const client = build(
      () =>
        jsonResponse(
          503,
          {
            detail: {
              code: 'DEPOSIT_TEMPORARILY_UNAVAILABLE',
              message: 'no USDT_TRC20 deposit address is free right now',
            },
          },
          { 'retry-after': '60' },
        ),
      { attempts: 1 },
    );
    await expect(
      client.createDeposit({ external_user_id: 'user-42', asset: 'USDT_TRC20' }),
    ).rejects.toMatchObject({ code: 'DEPOSIT_TEMPORARILY_UNAVAILABLE', retryAfter: 60 });
  });

  it("does not choke on a proxy's HTML error page", async () => {
    // The failure an integrator would otherwise report as an SDK bug.
    const client = build(
      () =>
        new Response('<html><body>Gateway Timeout</body></html>', {
          status: 504,
          headers: { 'content-type': 'text/html' },
        }),
      { attempts: 1 },
    );
    await expect(client.getUserBalances('user-42')).rejects.toMatchObject({
      statusCode: 504,
      message: expect.stringContaining('Gateway Timeout'),
    });
  });
});

// -- reads ------------------------------------------------------------------

describe('reads', () => {
  it('leaves amounts and timestamps as strings', async () => {
    // A generator that mapped these to number or Date would round a satoshi
    // away and re-render a timestamp an integrator may be comparing.
    const client = build(() => jsonResponse(200, DEPOSIT_BODY));
    const deposit = await client.getDeposit('019feb96-7e52-771a-a8cb-a86dccc87339');
    expect(deposit.amount_expected).toBe('0.50000000');
    expect(deposit.created_at).toBe('2026-08-10T12:15:13+00:00');
  });

  it('treats a degraded readyz as an answer rather than an exception', async () => {
    // 503 there names the unhappy component; throwing would hide the body.
    const client = build(() =>
      jsonResponse(503, {
        status: 'degraded',
        components: [{ name: 'btcpay', status: 'down', detail: 'connect refused' }],
      }),
    );
    const ready = await client.readyz();
    expect(ready.status).toBe('degraded');
    expect(ready.components[0]?.name).toBe('btcpay');
  });

  it('puts a path parameter where it belongs', async () => {
    const client = build(() => jsonResponse(200, DEPOSIT_BODY));
    await client.getDeposit('019feb96-7e52-771a-a8cb-a86dccc87339');
    expect(seen[0]?.url).toBe(`${BASE_URL}/v1/deposits/019feb96-7e52-771a-a8cb-a86dccc87339`);
  });

  it('puts query parameters where they belong', async () => {
    const client = build(() => jsonResponse(200, { deposits: [], next_cursor: null }));
    await client.listUserDeposits('user-42', { limit: 5 });
    expect(seen[0]?.url).toBe(`${BASE_URL}/v1/users/user-42/deposits?limit=5`);
  });
});
