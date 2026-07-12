import { defineConfig } from "vite";

// Amplify Hosting can serve the app from any sub-path, so assets must be
// referenced relatively — hence base: "./" rather than the "/" default.
export default defineConfig({
  base: "./",
  server: {
    host: true,
    port: 3000,
  },
  preview: {
    host: true,
    port: 3000,
  },
  build: {
    outDir: "dist", // must match amplify.yml baseDirectory
  },
});
