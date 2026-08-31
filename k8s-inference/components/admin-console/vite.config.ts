import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { browserFixture } from "./src/test/browserFixtures.ts";

const fixturePlugin: Plugin = {
  name: "fs2-admin-browser-fixtures",
  configureServer(server) {
    server.middlewares.use((request, response, next) => {
      const path = request.url?.split("?", 1)[0] ?? "";
      if (!path.startsWith("/admin/api/v1/")) return next();
      if (request.method === "DELETE" && path === "/admin/api/v1/session") {
        response.statusCode = 204;
        response.end();
        return;
      }
      const fixture = browserFixture(path);
      if (fixture === undefined) {
        response.statusCode = 404;
        response.end(JSON.stringify({ title: "Fixture not found" }));
        return;
      }
      response.statusCode = 200;
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify(fixture));
    });
  },
  configurePreviewServer(server) {
    server.middlewares.use((request, response, next) => {
      const path = request.url?.split("?", 1)[0] ?? "";
      if (!path.startsWith("/admin/api/v1/")) return next();
      if (request.method === "DELETE" && path === "/admin/api/v1/session") {
        response.statusCode = 204;
        response.end();
        return;
      }
      const fixture = browserFixture(path);
      if (fixture === undefined) {
        response.statusCode = 404;
        response.end(JSON.stringify({ title: "Fixture not found" }));
        return;
      }
      response.statusCode = 200;
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify(fixture));
    });
  },
};

export default defineConfig(({ mode }) => ({
  base: "/admin/",
  plugins: [
    react(),
    ...(mode === "fixture" ? [fixturePlugin] : []),
  ],
  build: {
    outDir: "dist",
    sourcemap: true,
  },
  server: {
    proxy: {
      "/admin/api": "http://127.0.0.1:8080",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: true,
  },
}));
