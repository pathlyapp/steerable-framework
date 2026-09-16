/**
 * 产品 web 构建的共享 vite 配置工厂（2.3）。
 *
 * 每个产品（products/<id>/web/）有自己的 vite 入口：index.html +
 * main.tsx（静态注册本产品的包渲染层贡献）+ vite.config.ts（调本工厂）。
 * 产品的编译单元只含自己的包——未激活包代码不进 bundle，不依赖树摇
 * 正确性（物理隔离与 node 侧对称）。
 *
 * Electron 加载产物时走 file:// 协议，必须用相对路径，否则资源 404。
 * dev server 跑在固定端口，主进程的 IS_DEV 分支 loadURL('http://localhost:5173')。
 */
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { existsSync, readFileSync } from 'node:fs';

const HERE = path.dirname(fileURLToPath(import.meta.url));

export interface ProductViteConfigOptions {
  /** 产品目录（products/<id>/web 的上一级…即 products/<id>）。 */
  productDir: string;
  /** 产品 flavor（决定品牌 define 与激活包集合）。 */
  flavor: string;
}

export function createProductViteConfig(options: ProductViteConfigOptions) {
  const { productDir, flavor } = options;
  const repoRoot = path.resolve(productDir, '../..');
  // shell 渲染层源码锚到本包自身（3.2 起住在框架仓库
  // packages/agent-shell/web/src），产品经 `@/` 别名引用。
  const webSrcDir = path.resolve(HERE, 'src');
  const readJson = (p: string) => JSON.parse(readFileSync(p, 'utf8'));

  // 产品品牌（3.1）：product.json 的 brand 字段优先（无包产品的品牌
  // 来源）；否则取激活包 pack.json 的 brand（0.3g 机制，读
  // products/manifest.json 的 flavor→包映射，第一个带 brand 的包）。
  // 都没有则用 shell 中性默认（Agent Shell）。
  const productJsonPath = path.join(productDir, 'product.json');
  const product = existsSync(productJsonPath) ? readJson(productJsonPath) : {};
  const manifest = readJson(path.join(repoRoot, 'products/manifest.json'));
  let packBrand: Record<string, string> | null = null;
  for (const packId of manifest.flavors?.[flavor] ?? []) {
    const pack = readJson(path.join(repoRoot, 'packages', `pack-${packId}`, 'pack.json'));
    if (pack.brand) {
      packBrand = pack.brand;
      break;
    }
  }
  const brand = product.brand ?? packBrand;

  return defineConfig({
    // root 默认取 config 文件所在目录（products/<id>/web）。
    base: './',
    plugins: [react(), tailwindcss()],
    define: {
      // 显式 define 让 flavor 条件在构建期可静态折叠。
      'import.meta.env.VITE_APP_FLAVOR': JSON.stringify(flavor),
      // 品牌 define：产品/包声明覆盖 shell 中性默认（Agent Shell）。
      // logo 不走 define——资产必须静态 import 才进 bundle，由包 web 模块
      // 在注册时经 setBrandLogoUrl 注入（本包 src/brand.ts）。
      'import.meta.env.VITE_BRAND_NAME': JSON.stringify(brand?.displayName ?? 'Steerable Shell'),
      'import.meta.env.VITE_BRAND_TAGLINE': JSON.stringify(brand?.tagline ?? '一款本地桌面 AI 伙伴'),
      'import.meta.env.VITE_DEFAULT_AGENT_ID': JSON.stringify(brand?.defaultAgentId ?? 'local-assistant'),
    },
    resolve: {
      alias: {
        // shell 渲染层源码（产品入口与包 web 模块都以 `@/` 引用它）。
        '@': webSrcDir,
      },
    },
    // 静态资产（favicon 等中性默认）随 shell web 包分发；产品可在自己的
    // web root 放 public/ 覆盖（vite 不合并双 publicDir——需要产品级资产
    // 时在产品 vite.config 里显式覆盖本字段）。
    publicDir: path.resolve(HERE, 'public'),
    server: {
      port: 5173,
      strictPort: true,
      host: '127.0.0.1',
      // 产品 root 在 products/<id>/web；shell 渲染层源码在框架仓库
      // （3.2 起经 file: 依赖符号链接）——两个根都放行。
      fs: { allow: [repoRoot, path.resolve(webSrcDir, '..')] },
    },
    build: {
      // 产物落在产品目录：products/<id>/web/dist（打包期被 electron-builder
      // 映射为包内 web-dist/）。
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: true,
      target: 'chrome120',
    },
  });
}
