/* Creado por Aldo Garcia. */
/**
 * Configuracion de Vite para Matrix RH.
 *
 * En desarrollo el proxy manda /api al backend para que el navegador vea un
 * unico origen: es lo que hace que la cookie de sesion HttpOnly + SameSite
 * funcione igual en dev que en produccion (patron BFF).
 */
// Se importa defineConfig de "vitest/config" y no de "vite": es la unica que
// tipa la clave `test`. Con la de "vite" el bloque de pruebas queda sin
// verificacion de tipos y un error de configuracion pasa desapercibido.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Sin sourcemaps en el build distribuible: no se publica el codigo original.
    sourcemap: false,
    target: "es2020",
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.tsx", "tests/**/*.test.ts"],
  },
});
