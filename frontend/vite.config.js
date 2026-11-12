import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        dashboard: fileURLToPath(new URL("index.html", import.meta.url)),
        replay: fileURLToPath(new URL("replay.html", import.meta.url)),
      },
    },
  },
});
