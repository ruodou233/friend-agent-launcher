import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const hasBuildGateway = Boolean(env.FRIEND_GATEWAY_URL?.trim());
  return {
    clearScreen: false,
    envPrefix: ["VITE_"],
    define: {
      __BUILD_VARIANT__: JSON.stringify(mode === "codex" ? "codex" : "claude"),
      // The URL is non-sensitive build metadata; Rust independently fails closed
      // in release mode if FRIEND_GATEWAY_URL is not supplied to Cargo.
      __BUILD_GATEWAY_CONFIGURED__: JSON.stringify(hasBuildGateway),
    },
    server: {
      port: 1420,
      strictPort: true,
    },
  };
});
