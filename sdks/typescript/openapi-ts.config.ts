import { defineConfig } from '@hey-api/openapi-ts';

/**
 * How the generated core is produced. Read alongside sdks/README.md.
 *
 * `src/generated` is never edited by hand: CI regenerates it and fails on any
 * diff, so a hand-fix would be undone by the next run and the client would
 * silently disagree with the server again.
 *
 * The input is the committed OpenAPI document, not a running server. That is
 * what makes generation reproducible on a laptop with nothing else installed,
 * and it is drift-gated on the server side, so it cannot describe an API that
 * does not exist.
 */
export default defineConfig({
  input: '../../docs/reference/openapi.json',
  output: {
    path: 'src/generated',
    // Explicit rather than inferred from tsconfig. The package is ESM, so the
    // emitted JavaScript must carry real `.js` specifiers or Node cannot
    // resolve them at runtime — and inferring it means the committed output
    // depends on a tsconfig lookup that a generation job might not repeat.
    module: { extension: '.js' },
    // No formatter and no linter in the pipeline. Both would put a second
    // pinned toolchain between the spec and the committed output, and a
    // formatter that resolves differently on two machines turns the drift
    // gate into a coin toss.
    postProcess: [],
  },
  plugins: [
    // The fetch client is emitted into the output rather than installed, so a
    // published package pulls in no runtime dependency at all.
    '@hey-api/client-fetch',
    {
      name: '@hey-api/typescript',
      // Every amount and every timestamp in this API is a string. Nothing here
      // may turn one into a number or a Date: `21e6` BTC in satoshis is past
      // JavaScript's safe integer range, and re-rendering a timestamp changes
      // bytes an integrator may be comparing.
      enums: 'javascript',
    },
    {
      name: '@hey-api/sdk',
      // The facade in src/client.ts owns errors, retries and idempotency, so
      // the generated layer stays a thin typed request per operation.
      client: true,
      // `fields` keeps the response object beside the body, which is what the
      // facade needs: the status code and `Retry-After` decide whether a call
      // is retried, and `data` alone cannot answer that.
      responseStyle: 'fields',
      // Explicit, because the default changing would be silent and wrong here.
      // A transformer is what turns an ISO string into a Date, and every
      // timestamp in this API is a string that must reach the caller unchanged.
      transformer: false,
    },
  ],
});
