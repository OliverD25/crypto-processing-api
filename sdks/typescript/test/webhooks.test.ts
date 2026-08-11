/**
 * The webhook helpers, beyond what the shared vectors already pin.
 *
 * The vectors cover agreement with the server. These cover the shape of the
 * helper itself: what it accepts as a header — including the two shapes Node
 * hands you — what it throws, and what a caller gets back to branch on.
 */

import { createHmac } from 'node:crypto';

import { describe, expect, it } from 'vitest';

import {
  UnknownEventTypeError,
  WebhookVerificationError,
  parseEvent,
  verifyWebhook,
} from '../src/index.js';

const SECRET = 'not-a-real-secret-for-a-test';
const BODY = Buffer.from(
  '{"created_at":"2026-08-10T13:11:02+00:00",' +
    '"data":{"amount_credited":"0.50000000","asset":"BTC",' +
    '"deposit_id":"019feb96-7e52-771a-a8cb-a86dccc87339",' +
    '"external_user_id":"user-42","status":"settled"},' +
    '"id":"evt_019f3c1e-0a2b-7c4d-8e5f-6a7b8c9d0e1f","type":"deposit.settled"}',
  'utf-8',
);
const NOW = 1_760_000_000;

/** Node's own crypto, so the test is not the implementation checking itself. */
function headerFor(body: Buffer = BODY, secret = SECRET, timestamp = NOW): string {
  const digest = createHmac('sha256', secret)
    .update(Buffer.concat([Buffer.from(`${timestamp}.`, 'utf-8'), body]))
    .digest('hex');
  return `t=${timestamp},v1=${digest}`;
}

describe('accepting headers', () => {
  it('takes a fetch Headers object', async () => {
    const headers = new Headers({ 'X-CPA-Signature': headerFor() });
    await expect(verifyWebhook(BODY, headers, SECRET, { now: NOW })).resolves.toBe(true);
  });

  it('takes a Node header object, which is lowercased', async () => {
    const headers = { 'x-cpa-signature': headerFor(), host: 'example.test' };
    await expect(verifyWebhook(BODY, headers, SECRET, { now: NOW })).resolves.toBe(true);
  });

  it('takes a repeated header, which Node presents as an array', async () => {
    const headers = { 'x-cpa-signature': [headerFor()] };
    await expect(verifyWebhook(BODY, headers, SECRET, { now: NOW })).resolves.toBe(true);
  });

  it('takes the value on its own', async () => {
    await expect(verifyWebhook(BODY, headerFor(), SECRET, { now: NOW })).resolves.toBe(true);
  });

  it('refuses a missing header rather than crashing', async () => {
    await expect(verifyWebhook(BODY, {}, SECRET, { now: NOW })).resolves.toBe(false);
    await expect(verifyWebhook(BODY, undefined, SECRET, { now: NOW })).resolves.toBe(false);
    await expect(verifyWebhook(BODY, null, SECRET, { now: NOW })).resolves.toBe(false);
  });
});

describe('what must never verify', () => {
  it('an unconfigured secret refuses everything', async () => {
    // The dangerous failure: an empty secret verifying whatever arrives.
    await expect(verifyWebhook(BODY, headerFor(BODY, ''), '', { now: NOW })).resolves.toBe(false);
  });

  it('a re-serialized body does not match', async () => {
    // The trap the docs lead with. In Node it hides better than elsewhere:
    // `JSON.stringify(JSON.parse(x))` happens to reproduce this server's
    // compact output byte for byte, so the naive version passes in testing and
    // fails on the first payload whose rendering differs. Anything that
    // re-renders with whitespace — a pretty-printer, a logging middleware that
    // hands on what it formatted — is already broken.
    const reserialized = Buffer.from(
      JSON.stringify(JSON.parse(BODY.toString('utf-8')), null, 2),
      'utf-8',
    );
    expect(reserialized.equals(BODY)).toBe(false);
    await expect(verifyWebhook(reserialized, headerFor(), SECRET, { now: NOW })).resolves.toBe(
      false,
    );
  });

  it('the parsed object is not a body', async () => {
    // `express.json()` hands you an object. Stringifying it with the default
    // `String()` gives "[object Object]", which must not verify as anything.
    const parsed = JSON.parse(BODY.toString('utf-8')) as unknown;
    await expect(
      verifyWebhook(String(parsed), headerFor(), SECRET, { now: NOW }),
    ).resolves.toBe(false);
  });

  it('a body given as a string still verifies, and a changed one does not', async () => {
    const asText = BODY.toString('utf-8');
    await expect(verifyWebhook(asText, headerFor(), SECRET, { now: NOW })).resolves.toBe(true);
    await expect(
      verifyWebhook(asText.replace('0.50000000', '5.00000000'), headerFor(), SECRET, { now: NOW }),
    ).resolves.toBe(false);
  });
});

describe('parseEvent', () => {
  it('returns the typed envelope', async () => {
    const event = await parseEvent(BODY, { 'x-cpa-signature': headerFor() }, SECRET, { now: NOW });
    expect(event.type).toBe('deposit.settled');
    if (event.type === 'deposit.settled') {
      // The literal `type` narrows the union, which is the point of it.
      expect(event.data.amount_credited).toBe('0.50000000');
    }
  });

  it('refuses an unsigned body', async () => {
    await expect(
      parseEvent(BODY, 't=1,v1=deadbeef', SECRET, { now: NOW }),
    ).rejects.toBeInstanceOf(WebhookVerificationError);
  });

  it('refuses a body that was changed after signing', async () => {
    const tampered = Buffer.from(BODY.toString('utf-8').replace('0.50000000', '5.00000000'));
    await expect(parseEvent(tampered, headerFor(), SECRET, { now: NOW })).rejects.toBeInstanceOf(
      WebhookVerificationError,
    );
  });

  it('gives an unknown event type its own error', async () => {
    // New types arrive in any minor. Skipping one must not look like a forgery.
    const body = Buffer.from(
      BODY.toString('utf-8').replace('"deposit.settled"', '"deposit.invented_next_year"'),
    );
    await expect(parseEvent(body, headerFor(body), SECRET, { now: NOW })).rejects.toThrow(
      UnknownEventTypeError,
    );
  });

  it('keeps an unknown field rather than rejecting it', async () => {
    // Additive changes must not break a deployed integration.
    const body = Buffer.from(
      BODY.toString('utf-8').replace('"type":"deposit.settled"', '"invented":1,"type":"deposit.settled"'),
    );
    const event = await parseEvent(body, headerFor(body), SECRET, { now: NOW });
    expect((event as unknown as { invented: number }).invented).toBe(1);
  });

  it('refuses a body that is not JSON', async () => {
    const body = Buffer.from('<html>gateway timeout</html>');
    await expect(parseEvent(body, headerFor(body), SECRET, { now: NOW })).rejects.toBeInstanceOf(
      WebhookVerificationError,
    );
  });

  it('refuses an envelope missing a field', async () => {
    const body = Buffer.from('{"type":"deposit.settled"}');
    await expect(parseEvent(body, headerFor(body), SECRET, { now: NOW })).rejects.toBeInstanceOf(
      WebhookVerificationError,
    );
  });
});
