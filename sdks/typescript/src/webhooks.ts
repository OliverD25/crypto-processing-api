/**
 * Verify and parse a delivered webhook.
 *
 * This is a port of the server's `core/signing.py`, and the two are held
 * together by `sdks/signature-vectors.json` — the same cases run against both,
 * so they cannot quietly disagree.
 *
 * Built on WebCrypto, so the same code runs on Node 20+, Deno, Bun,
 * Cloudflare Workers and a browser. WebCrypto's digest API is asynchronous
 * everywhere, which is why `verifyWebhook` returns a promise.
 *
 * Three things that are easy to get wrong, and two more that Node adds:
 *
 * 1. **The signature covers the raw request body bytes.** Parsing the JSON and
 *    re-serializing it changes the whitespace, and the signature will never
 *    match.
 * 2. **The comparison is constant time.** `===` on a hex digest leaks how much
 *    of it was right.
 * 3. **The timestamp is checked.** A signature proves authenticity; only the
 *    window proves freshness.
 * 4. **Express parses the body before you see it.** `express.json()` hands you
 *    an object, and re-serializing it can never match. Mount
 *    `express.raw({ type: 'application/json' })` on the webhook route, or pass
 *    `verify: (req, _res, buf) => { req.rawBody = buf; }`.
 * 5. **Node's `timingSafeEqual` throws on a length mismatch.** The comparison
 *    here checks length first and returns false, rather than raising into a
 *    500 that looks like an outage.
 */

import { UnknownEventTypeError, WebhookVerificationError } from './errors.js';
import { EVENT_TYPES, type PlatformEvent } from './event-types.js';

/** The header the server signs with. */
export const SIGNATURE_HEADER = 'X-CPA-Signature';

/**
 * How far the signed timestamp may be from your clock, in seconds. Stripe's
 * convention and what the server enforces on its side.
 */
export const REPLAY_WINDOW_SECONDS = 300;

/** Whatever shape of headers your framework hands you. */
export type HeaderSource =
  | string
  | null
  | undefined
  | Headers
  | Record<string, string | string[] | undefined>;

/** The body as it arrived. A string is encoded as UTF-8. */
export type BodySource = Uint8Array | ArrayBuffer | string;

export interface VerifyOptions {
  /** Override the replay window. Rarely a good idea. */
  toleranceSeconds?: number;
  /** Unix seconds to check the timestamp against. Defaults to the clock. */
  now?: number;
}

function headerValue(headers: HeaderSource): string | null {
  if (headers === null || headers === undefined) return null;
  if (typeof headers === 'string') return headers;
  if (typeof (headers as Headers).get === 'function') {
    return (headers as Headers).get(SIGNATURE_HEADER);
  }
  // Node lowercases incoming header names; other runtimes do not. Neither
  // assumption is safe, so look without caring.
  const wanted = SIGNATURE_HEADER.toLowerCase();
  for (const [name, value] of Object.entries(headers as Record<string, string | string[] | undefined>)) {
    if (name.toLowerCase() !== wanted) continue;
    if (Array.isArray(value)) return value[0] ?? null;
    return value ?? null;
  }
  return null;
}

function toBytes(body: BodySource): Uint8Array {
  if (typeof body === 'string') return new TextEncoder().encode(body);
  if (body instanceof Uint8Array) return body;
  return new Uint8Array(body);
}

/** Length first, then every byte. No early exit once the lengths agree. */
function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let index = 0; index < a.length; index += 1) {
    difference |= a.charCodeAt(index) ^ b.charCodeAt(index);
  }
  return difference === 0;
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

async function hmacHex(secret: string, message: Uint8Array): Promise<string> {
  const key = await globalThis.crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  // `as BufferSource` because a Uint8Array over a SharedArrayBuffer is not one,
  // and TypeScript cannot tell which we have.
  return toHex(await globalThis.crypto.subtle.sign('HMAC', key, message as BufferSource));
}

/**
 * True if `body` was signed by `secret` and the signature is still fresh.
 *
 * `body` must be the bytes that arrived, not a re-serialization of the parsed
 * JSON. `headers` may be a `Headers`, a Node header object, or the value of
 * `X-CPA-Signature` on its own.
 */
export async function verifyWebhook(
  body: BodySource,
  headers: HeaderSource,
  secret: string,
  options: VerifyOptions = {},
): Promise<boolean> {
  // An unconfigured secret must reject everything. The opposite mistake —
  // treating "no secret" as "no checking" — turns the endpoint into an open
  // one and looks like it is working.
  if (!secret) return false;
  const header = headerValue(headers);
  if (!header) return false;

  let rawTimestamp: string | undefined;
  let presented: string | undefined;
  for (const piece of header.split(',')) {
    const at = piece.indexOf('=');
    if (at === -1) continue;
    const name = piece.slice(0, at);
    const value = piece.slice(at + 1);
    // First wins, so an appended duplicate cannot displace the real one.
    if (name === 't' && rawTimestamp === undefined) rawTimestamp = value;
    // Unknown elements are ignored rather than refused: versioning.md promises
    // a future scheme ships as a `v2=` beside this one, not instead of it.
    if (name === 'v1' && presented === undefined) presented = value;
  }
  if (!rawTimestamp || !presented) return false;
  if (!/^-?\d+$/.test(rawTimestamp)) return false;
  const timestamp = Number(rawTimestamp);
  if (!Number.isSafeInteger(timestamp)) return false;

  const tolerance = options.toleranceSeconds ?? REPLAY_WINDOW_SECONDS;
  const now = options.now ?? Math.floor(Date.now() / 1000);
  if (Math.abs(now - timestamp) > tolerance) return false;

  const bodyBytes = toBytes(body);
  const prefix = new TextEncoder().encode(`${timestamp}.`);
  const message = new Uint8Array(prefix.length + bodyBytes.length);
  message.set(prefix, 0);
  message.set(bodyBytes, prefix.length);

  return timingSafeEqualHex(await hmacHex(secret, message), presented);
}

/**
 * Verify, then return the event as a typed object.
 *
 * Throws `WebhookVerificationError` if the signature does not check out —
 * answer 401 and do nothing else. Throws `UnknownEventTypeError` for a `type`
 * this version does not know, which is the documented "acknowledge and skip"
 * case rather than a failure.
 *
 * Unknown *fields* are kept, not rejected. Parsing defensively is the
 * contract; a validating parser is the thing that breaks on the release that
 * adds a field.
 */
export async function parseEvent(
  body: BodySource,
  headers: HeaderSource,
  secret: string,
  options: VerifyOptions = {},
): Promise<PlatformEvent> {
  if (!(await verifyWebhook(body, headers, secret, options))) {
    throw new WebhookVerificationError(
      'the X-CPA-Signature header did not verify against the body and secret',
    );
  }
  return parseEventUnverified(body);
}

/**
 * Parse an event whose signature you have already checked.
 *
 * Separate and awkwardly named on purpose. Verification is not optional, and a
 * helper called `parseEvent` that skipped it would be the easiest possible
 * mistake to make.
 */
export function parseEventUnverified(body: BodySource): PlatformEvent {
  const text = typeof body === 'string' ? body : new TextDecoder().decode(toBytes(body));
  let document: unknown;
  try {
    document = JSON.parse(text);
  } catch (cause) {
    throw new WebhookVerificationError('the delivered body is not JSON', { cause });
  }
  if (typeof document !== 'object' || document === null || Array.isArray(document)) {
    throw new WebhookVerificationError('the delivered body is not a JSON object');
  }

  const envelope = document as Record<string, unknown>;
  const eventType = envelope['type'];
  if (typeof eventType !== 'string') {
    throw new WebhookVerificationError('the delivered event has no `type`');
  }
  if (!(EVENT_TYPES as readonly string[]).includes(eventType)) {
    throw new UnknownEventTypeError(eventType);
  }
  for (const field of ['id', 'created_at', 'data'] as const) {
    if (!(field in envelope)) {
      throw new WebhookVerificationError(`the delivered event has no \`${field}\``);
    }
  }
  return envelope as unknown as PlatformEvent;
}
