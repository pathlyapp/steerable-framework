import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

// 中性独立 web 应用构建（非产品路径）：root 是 app/，@ 别名锚到本包 src/，
// 不注入任何 VITE_BRAND_* define——brand.ts 的中性默认（Steerable Shell）
// 自动生效。产物在 app/dist/，由 @steerable/agent-shell 的 `web`/`client`
// 脚本经 DEEPPATH_WEB_DIST 指给 BS server / Electron。
export default defineConfig({
  root: path.join(HERE, 'app'),
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(HERE, 'src'),
    },
  },
  publicDir: path.resolve(HERE, 'public'),
  build: {
    outDir: path.resolve(HERE, 'app', 'dist'),
    emptyOutDir: true,
  },
  server: {
    port: 5174,
    strictPort: true,
    host: '127.0.0.1',
    fs: { allow: [HERE] },
  },
});
