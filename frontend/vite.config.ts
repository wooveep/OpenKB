import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Dev server proxies /api to the OpenKB REST API on :7566; production
// serves the built bundle from the same origin via FastAPI StaticFiles.
// outDir MUST stay ../openkb/web — the wheel packages it from there
// (see pyproject.toml [tool.hatch.build] artifacts).
export default defineConfig(({ mode }) => ({
  base: "/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: mode === "desktop" ? "../openkb/desktop-web" : "../openkb/web",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:7566",
        changeOrigin: true,
      },
    },
  },
}))
