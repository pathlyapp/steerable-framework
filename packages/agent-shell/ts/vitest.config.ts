import { defineConfig } from 'vitest/config';

// agent-shell 的单元/集成测试（tests/）。e2e（真实 sidecar + loopback
// mock LLM 的整产品旅程）留在应用仓库侧——它们经产品组装根驱动 shell。
export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
  },
});
