import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// The Desktop shell loads this bundle directly. Keep it outside the Python
// package so Python distributions cannot accidentally expose a browser UI.
export default defineConfig({
  base: "/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
  },
})
