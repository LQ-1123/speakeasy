import { defineConfig } from "vitest/config";

/**
 * Vitest runs application modules through Vite, but externalises `node_modules`
 * by default. `@material/material-color-utilities` is an ESM-only package whose
 * emitted `.js` files use extensionless relative imports (e.g.
 * `./dynamiccolor/dynamic_color`), which Node's native ESM loader cannot
 * resolve. Next.js/webpack resolves them for the app build; for unit tests the
 * package must be transformed by Vite instead, so it is inlined here.
 */
export default defineConfig({
  test: {
    server: {
      deps: {
        inline: ["@material/material-color-utilities"],
      },
    },
  },
});
