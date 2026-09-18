import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';

// shell 渲染层组件测试：happy-dom 提供 DOM；@ 别名锚到本包 src/。
// 场景包的 web 测试在应用仓库侧运行（它们与包代码同住）。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'happy-dom',
    include: ['src/**/*.test.{ts,tsx}'],
    // Node ≥22.4 下 happy-dom 的 localStorage 进不了全局，见 vitest.setup.ts。
    setupFiles: ['./vitest.setup.ts'],
  },
});
