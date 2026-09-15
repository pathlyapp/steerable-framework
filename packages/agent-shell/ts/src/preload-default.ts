/**
 * shell 默认 preload 入口：无包贡献的中性面（buildPreloadApi()）。
 * 由本包构建（esbuild → dist/preload.cjs）打包成单文件 CJS，供
 * shell main.ts 的默认 preload 路径与纯 shell/demo 启动使用。
 * 带 invoke 型包贡献的产品用自己的 preload 入口（产品构建打包到
 * 产品 main.js 旁）并经 setPreloadPath 注入，不走本入口。
 */
import { buildPreloadApi } from './preload.js';

buildPreloadApi();
