import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    plugins: [react()],
    server: { host: '127.0.0.1', port: 5173, strictPort: true,
      proxy: { '/api': { target: env.WANDER_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true } } },
  };
});
