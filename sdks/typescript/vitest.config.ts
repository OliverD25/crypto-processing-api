import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['test/**/*.test.ts'],
    // `dist` holds a compiled copy of everything in `src`. Without this a
    // build before a test run makes every suite appear to run twice.
    exclude: ['node_modules/**', 'dist/**'],
  },
});
