import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Vite plugin to strip `type="module"` from built HTML script tags.
 * Streamlit v1 component iframes serve files via their own static server
 * and module scripts can fail to load in that context.
 */
function stripModuleType(): Plugin {
  return {
    name: "strip-module-type",
    enforce: "post",
    transformIndexHtml(html) {
      // Replace module script with deferred regular script.
      // `defer` ensures the script runs after the DOM is parsed,
      // matching the behavior of type="module" but without CORS issues.
      return html.replace(
        /<script type="module" crossorigin/g,
        "<script defer",
      );
    },
  };
}

export default defineConfig({
  plugins: [react(), stripModuleType()],
  base: "./",
  build: {
    outDir: "dist",
    assetsDir: ".",
    rollupOptions: {
      output: {
        entryFileNames: "index.js",
        assetFileNames: "[name][extname]",
        format: "iife",
      },
    },
  },
  server: { port: 5173 },
});
