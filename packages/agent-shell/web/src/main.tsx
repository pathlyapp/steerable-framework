import { StrictMode, lazy, Suspense, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { createHashRouter, RouterProvider } from 'react-router-dom';
import { AppShell } from './AppShell';
import { AgentLayout } from './layouts/AgentLayout';
import { AgentPage } from './pages/AgentPage';
import { SettingsPage } from './pages/SettingsPage';
import { getPackRoutes } from './packs/registry';
import { getBrandLogoUrl, BRAND_NAME } from './brand';
import { getAppShellGate } from './auth/gate';
import './styles/index.css';

// 浏览器 dev mock 的安装在 bootstrap() 开头（动态 import + DEV 门）。

// Dev-only preview harness. Lazy-loaded behind `import.meta.env.DEV` so the
// chunk vanishes from production builds entirely — Rollup tree-shakes the
// dynamic import when the condition is statically `false`. The route itself
// is also conditionally registered; in prod a stray `#/preview/chat` URL
// falls through to react-router's default "no match" handling (the layout
// outlet just renders nothing).
const ChatPanelPreviewPage = import.meta.env.DEV
  ? lazy(() => import('./pages/ChatPanelPreviewPage'))
  : null;

// 用 HashRouter 而不是 BrowserRouter：Electron loadFile() 加载本地 index.html
// 时 URL 是 file://... 没有真正的 server 解析路径段，BrowserRouter 在刷新或
// 跳转时会 404。HashRouter 把路由放进 # 后面，纯客户端解析，跟 file:// 兼容。
//
// 路由分层：
//   AppShell                 全局顶层 chrome（浏览器预览提示等）
//     ├─ AgentLayout         带 sidebar 的主界面（终端是它的内嵌面板，不是路由）
//     │    ├─ /              默认进 AgentPage（无 chatId）
//     │    ├─ /agent
//     │    ├─ /agent/:chatId
//     │    └─ /settings      设置页（智能体 / Skill / MCP / 综合，按 ?section= 分页面）
//     └─ <包路由>            场景包注册的独立页（如包的调试日志窗）
/**
 * web 应用 bootstrap（2.3 起由产品入口调用）。
 *
 * shell 本体不装载任何包的渲染层贡献：产品入口
 * （products/<id>/web/main.tsx）先静态调用包的 register*Renderer()，
 * 再调本函数——路由注册发生在路由表创建之前。
 */
function renderApplication(root: Root): void {
  const router = createHashRouter([
    {
      path: '/',
      element: <AppShell />,
      children: [
        {
          element: <AgentLayout />,
          children: [
            { index: true, element: <AgentPage /> },
            { path: 'agent', element: <AgentPage /> },
            { path: 'agent/:chatId', element: <AgentPage /> },
            { path: 'settings', element: <SettingsPage /> },
          ],
        },
        ...getPackRoutes().map(({ path, Component }) => ({
          path,
          element: <Component />,
        })),
        // Dev-only visual harness. Route is omitted entirely in prod builds so
        // the chunk doesn't ship to end users; `ChatPanelPreviewPage` is
        // intentionally not exported from any production code path either.
        ...(ChatPanelPreviewPage
          ? [
              {
                path: 'preview/chat',
                element: (
                  <Suspense fallback={(<div>loading preview…</div>) as ReactNode}>
                    <ChatPanelPreviewPage />
                  </Suspense>
                ),
              },
            ]
          : []),
      ],
    },
  ]);

  root.render(
    <StrictMode>
      <RouterProvider router={router} />
    </StrictMode>,
  );
}

export async function bootstrap(): Promise<void> {
  if (import.meta.env.DEV) {
    // 动态 import：prod 构建里 Rollup 把 DEV 分支连同 mock（含其 fixtures）
    // 整体树摇掉，浏览器 dev mock 数据不进产物。
    const { installBrowserDevElectronMock } = await import('./lib/browser-dev-electron-mock');
    installBrowserDevElectronMock();
  }

  // 标题与 favicon 在 bootstrap 时设置——此刻产品入口已完成包注册
  // （品牌 logo 由包经 setBrandLogoUrl 注入，见 brand.ts）。
  document.title = BRAND_NAME;
  document.querySelector<HTMLLinkElement>('link[rel="icon"]')?.setAttribute('href', getBrandLogoUrl());

  const rootEl = document.getElementById('root');
  if (!rootEl) {
    throw new Error('Missing #root element in index.html');
  }
  const root = createRoot(rootEl);
  const gate = getAppShellGate();
  if (gate?.enabled()) {
    let authenticated = false;
    const onAuthenticated = () => {
      if (authenticated) return;
      authenticated = true;
      renderApplication(root);
    };
    const Gate = gate.Component;
    root.render(
      <StrictMode>
        <Gate onAuthenticated={onAuthenticated} />
      </StrictMode>,
    );
    return;
  }

  renderApplication(root);
}
