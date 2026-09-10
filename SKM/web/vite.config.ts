import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

/** Context path を末尾 slash なしの絶対 path として検証する。 */
function normalizeContextPath(value: string): string {
  const normalized = value.trim().replace(/\/+$/, '')
  if (!/^\/[A-Za-z0-9._~-]+(?:\/[A-Za-z0-9._~-]+)*$/.test(normalized)) {
    throw new Error(`SKILLMIND_CONTEXT_PATH must be an absolute path without a trailing slash: ${value}`)
  }
  return normalized
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  const contextPath = normalizeContextPath(env.SKILLMIND_CONTEXT_PATH ?? '/skillmind')
  const apiPrefix = `${contextPath}/api`

  return {
    base: `${contextPath}/`,
    plugins: [react()],
    server: {
      port: 5173,
      // Local 開発でも外部 context path を再現し、Backend へ渡す直前にだけ除去する。
      proxy: {
        [apiPrefix]: {
          target: 'http://127.0.0.1:8000',
          changeOrigin: false,
          rewrite: (path: string) => path.slice(contextPath.length),
        },
      },
    },
    // DOM を必要としない API contract test は軽量な Node 環境で実行する。
    test: {
      environment: 'node',
      include: ['tests/**/*.test.{ts,tsx}'],
    },
  }
})
