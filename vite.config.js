import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const defaultModel =
    mode === "codex"
      ? env.FRIEND_CODEX_MODEL || env.FRIEND_DEFAULT_MODEL
      : env.FRIEND_CLAUDE_MODEL || env.FRIEND_DEFAULT_MODEL;
  return {
    clearScreen: false,
    envPrefix: ["VITE_"],
    define: {
      __BUILD_VARIANT__: JSON.stringify(mode === "codex" ? "codex" : "claude"),
      __DEFAULT_GATEWAY_URL__: JSON.stringify(env.FRIEND_GATEWAY_URL || ""),
      __DEFAULT_MODEL__: JSON.stringify(defaultModel || ""),
    },
    server: {
      port: 1420,
      strictPort: true,
    },
  };
});
