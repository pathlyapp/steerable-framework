import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

// 官网静态 demo 构建：root 是 app-demo/（入口先装浏览器 mock 再 bootstrap），
// base './' 让产物可挂在 GitHub Pages 的 /demo/ 子路径下。产物在
// app-demo/dist/，由 docs.yml 嵌进 mkdocs 站点的 _site/demo/。
export default defineConfig({
  root: path.join(HERE, 'app-demo'),
  base: './',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(HERE, 'src'),
    },
  },
  publicDir: path.resolve(HERE, 'public'),
  build: {
    outDir: path.resolve(HERE, 'app-demo', 'dist'),
    emptyOutDir: true,
  },
});
