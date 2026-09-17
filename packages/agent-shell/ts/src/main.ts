// 产品组装根（products/<id>/main.ts 或 devtools/dev-main.ts）必须先于本
// 模块完成 import：包迁移注册要先于 storage 单例构造（0.3b / 2.3）。
import { setSecondInstanceHandler } from './single-instance.js';
import {
  app,
  BrowserWindow,
  Menu,
  screen,
  shell,
  nativeTheme,
  ipcMain,
  clipboard,
  dialog,
  net as electronNet,
  Notification as ElectronNotification,
  session,
} from 'electron';
import { randomUUID } from 'crypto';
import { existsSync, promises as fsPromises } from 'fs';
import { homedir } from 'node:os';
import path from 'path';
import { fileURLToPath } from 'url';
import dotenv from 'dotenv';
import log from 'electron-log';
import { getBrand } from './brand.js';
import { getPreloadPath } from './runtime.js';
import { getProductConfig } from './product-config.js';
import { createJsonStore } from './json-store.js';
import { bindWorkspaceSkillRoots } from './local-backend/skill-loader.js';
import { getUserDataDir } from './runtime.js';
import { saveAttachmentFiles } from './attachments.js';

// Re-derive __filename / __dirname for ESM modules. Used below for resolving
// paths relative to the compiled main.js (preload, dotenv files, page HTML).
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Load env files BEFORE anything else reads process.env so the preload
// (which inherits process.env from the main process) can see them. Loading
// .env.local takes precedence over .env（与上游 Python 服务同约定）。
// Existing process.env values always win (override: false).
(function loadDotenvFiles(): void {
  // dist/src/main.js → 仓库根是 ../..（rootDir=仓库根，dist 镜像源码树）。
  const repoRoot = path.resolve(__dirname, '..', '..');
  for (const file of ['.env.local', '.env']) {
    const fullPath = path.join(repoRoot, file);
    if (existsSync(fullPath)) {
      dotenv.config({ path: fullPath, override: false });
    }
  }
})();

// 让 Electron 自身的 userData（electron-log 文件日志、Chromium 缓存等）
// 跟随 DEEPPATH_USER_DATA_DIR——否则只读 HOME 上 `pnpm dev` 的日志/缓存
// 仍会写 ~/.config/<appName> 并 EROFS。未设置时是 no-op（返回值就是
// 默认 userData）。
app.setPath('userData', getUserDataDir());

import {
  type LocalExecRequest,
  type LocalFileReadRequest,
  type LocalFileWriteRequest,
  type LocalOpenRequest,
  type CommandSafetyConfigPayload,
} from './local-executor.js';
import { type CreateLocalScriptInput } from './local-script-registry.js';
import { type TerminalSpawnOptions } from './terminal-manager.js';
import { getActiveCoreLoopStreamId } from './local-backend/coreloop-stream.js';
import { getSidecarSupervisor } from './llm/index.js';
import { localStore } from './storage/index.js';
import { createHostRuntime } from './host/runtime.js';
import { registerPackIpc } from './host/ipc.js';

interface WindowState {
  width: number;
  height: number;
  x?: number;
  y?: number;
  isMaximized: boolean;
}

const store = createJsonStore<{ windowState: WindowState }>({ name: 'config' });

const FORCE_BUNDLED_WEB = process.env.DEEPPATH_FORCE_BUNDLED_WEB === '1';
const USE_BUNDLED_WEB = app.isPackaged || FORCE_BUNDLED_WEB;
const IS_DEV = !USE_BUNDLED_WEB;
const WEB_DEV_URL = process.env.DEEPPATH_WEB_DEV_URL || 'http://127.0.0.1:5173';
const START_PATH = process.env.DEEPPATH_START_PATH || '/agent';

// 主图标路径——dev 时是仓库 assets/，打包后是 app.asar/assets/。
// Linux 必须显式给 BrowserWindow 一个 icon（不会自动从 .deb/.AppImage 继承）；
// Windows 一般用 .exe 自带的 ICO，但 dev 模式下传 PNG 能让任务栏立刻看到 logo；
// macOS 用 .icns（package.json 里 electron-builder 配置已指向 icon-mac.png）。
const APP_ICON_PATH = path.join(
  app.getAppPath(),
  'assets',
  getBrand().flavor === 'generic' ? 'icon-generic.png' : 'icon.png',
);

let mainWindow: BrowserWindow | null = null;
let isQuitting = false;
let retryTimer: NodeJS.Timeout | null = null;
let retryCount = 0;
const MAX_RETRY_COUNT = 10;
const RETRY_DELAYS = [2000, 5000, 10000, 30000];

// 全部宿主服务由共享 HostRuntime 装配（与 BS server 同一份，见
// src/host/runtime.ts 模块头——历史上两处逐行重复，BS 曾漏接 TaskService
// 导致模型退化成"假后台"）。CS 的差异只剩广播（IPC）与生命周期。
const runtime = createHostRuntime({
  broadcast: (channel, payload) => broadcastTerminalEvent(channel, payload),
  // LocalBackendRouter 的后台事件（chat-title-updated / suggested-replies）只推主窗口。
  // 没拿到 mainWindow 时静默丢——title 是 nice-to-have，不该让启动顺序影响功能。
  broadcastMain: (eventName, payload) => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.webContents.send(eventName, payload);
  },
  hasWindow: () =>
    BrowserWindow.getAllWindows().some((win) => !win.isDestroyed()),
  onLog: (line) => log.info('[sidecar]', line),
  taskSweepReason: '应用重启，任务流已中断',
});
const {
  localExecutor,
  localScriptRegistry,
  terminalManager,
  packHandles,
  localBackendRouter,
  approvalBridge,
  askUserBridge,
  maybeExecInTerminal,
} = runtime;

let routeWindow: BrowserWindow | null = null;
// 进行中的 agent 流（streamId → AbortController）。cancelStream IPC、
// 窗口销毁、应用退出时统一 abort，保证不会有"关了还在跑"的幽灵循环。
const activeStreamControllers = new Map<string, AbortController>();

// 终端已内嵌进主窗口（/terminal 路由），不再有独立终端窗。PTY 输出广播到
// 所有窗口——没挂载 TerminalView 的窗口没有 listener，天然忽略；主窗切走
// 路由后 xterm 卸载，回来时靠 `terminal:ensure` 的 replay buffer 补输出，
// 所以这里不需要任何"窗口就绪"排队机制。
function broadcastTerminalEvent(channel: string, payload: unknown): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.webContents.send(channel, payload);
  }
}

terminalManager.on('data', (sessionId: string, chunk: string) => {
  broadcastTerminalEvent('terminal:data', { sessionId, chunk });
});
terminalManager.on('exit', (sessionId: string, code: number, signal: string | null) => {
  broadcastTerminalEvent('terminal:exit', { sessionId, code, signal });
});
terminalManager.on('spawned', session => {
  broadcastTerminalEvent('terminal:spawned', session);
});


function getStartPath(): string {
  if (!START_PATH) return '/agent';
  return START_PATH.startsWith('/') ? START_PATH : `/${START_PATH}`;
}

function getBundledWebIndexPath(): string {
  // 2.3 起 web 产物按产品构建：dev/未打包运行时由产品入口注入
  // DEEPPATH_WEB_DIST（products/<id>/web/dist）；打包后 electron-builder
  // 把产品 web dist 映射为包内 web-dist/（全产品统一约定）。
  const injected = process.env.DEEPPATH_WEB_DIST;
  if (injected) return path.join(injected, 'index.html');
  return path.join(app.getAppPath(), 'web-dist', 'index.html');
}

async function loadWebRoute(win: BrowserWindow, routePath: string): Promise<void> {
  const route = routePath.startsWith('/') ? routePath : `/${routePath}`;
  if (IS_DEV) {
    const url = new URL(WEB_DEV_URL);
    url.hash = route;
    await win.loadURL(url.toString());
    return;
  }
  await win.loadFile(getBundledWebIndexPath(), { hash: route });
}

async function checkDevServerConnection(): Promise<boolean> {
  return new Promise(resolve => {
    const request = electronNet.request({ method: 'HEAD', url: WEB_DEV_URL });
    const timeoutId = setTimeout(() => {
      request.abort();
      resolve(false);
    }, 3000);
    request.on('response', response => {
      clearTimeout(timeoutId);
      resolve((response.statusCode || 0) > 0);
    });
    request.on('error', () => {
      clearTimeout(timeoutId);
      resolve(false);
    });
    request.on('abort', () => {
      clearTimeout(timeoutId);
      resolve(false);
    });
    request.end();
  });
}

async function loadOfflinePage(): Promise<void> {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const offlinePath = path.join(__dirname, '../pages/offline.html');
  await mainWindow.loadFile(offlinePath, { query: { brand: getBrand().displayName } });
  mainWindow.webContents.send('network-status-changed', { online: false });
  scheduleRetry();
}

function scheduleRetry(): void {
  if (retryTimer) clearTimeout(retryTimer);
  if (retryCount >= MAX_RETRY_COUNT) return;
  const delayMs = RETRY_DELAYS[Math.min(retryCount, RETRY_DELAYS.length - 1)];
  retryTimer = setTimeout(async () => {
    retryCount++;
    const isUp = await checkDevServerConnection();
    if (isUp) {
      await loadApp();
    } else {
      scheduleRetry();
    }
  }, delayMs);
}

async function loadApp(): Promise<void> {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (IS_DEV) {
    try {
      await loadWebRoute(mainWindow, getStartPath());
      retryCount = 0;
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      mainWindow.webContents.send('network-status-changed', { online: true });
    } catch {
      await loadOfflinePage();
    }
    return;
  }

  try {
    await loadWebRoute(mainWindow, getStartPath());
    mainWindow.webContents.send('network-status-changed', { online: true });
  } catch {
    dialog.showErrorBox('应用启动失败', '内置前端资源可能缺失或损坏，请重新安装。');
    app.quit();
  }
}

function applyStrictCsp(): void {
  // Dev 模式下不强加 CSP：Next.js dev server 的静态资源 MIME 经常是 text/plain，
  // strict CSP + nosniff 会把所有 CSS/JS 全部拒掉，页面卡在 SSR fallback。
  // Bundled production 模式才应用 CSP。现在生产模式走 file:// + Vite 打包产物，
  // 需要允许 file: 源；connect 只放行本机 Ollama（用户可在设置里改 URL）。
  if (IS_DEV) {
    return;
  }
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    const csp = [
      "default-src 'self' file:",
      "connect-src 'self' file: http://127.0.0.1:11434",
      "img-src 'self' data: blob:",
      "style-src 'self' 'unsafe-inline'",
      "script-src 'self' 'unsafe-eval' 'unsafe-inline'",
      "font-src 'self' data:",
    ].join('; ');
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [csp],
      },
    });
  });
}

function createWindow(): void {
  // 出厂默认窗口按主屏工作区缩放（ capped ）：480×720 的手机尺寸在桌面端
  // 太窄，侧栏 + 对话区展不开。用户 resize/move 后持久化，之后的启动走
  // 已存状态，不再回这里。
  const workArea = screen.getPrimaryDisplay().workAreaSize;
  const defaultState: WindowState = {
    width: Math.min(1280, Math.max(720, Math.round(workArea.width * 0.7))),
    height: Math.min(860, Math.max(600, Math.round(workArea.height * 0.85))),
    isMaximized: false,
  };
  const savedState = store.get('windowState', defaultState);
  // 老版本出厂默认是 480×720，且 close 时无条件持久化——没调整过窗口的
  // 老安装也存着这个值。把它视为"从未自定义"，升级后跟随新默认。
  const isLegacyDefault =
    savedState.width === 480 && savedState.height === 720 && !savedState.isMaximized;
  const windowState = isLegacyDefault ? defaultState : savedState;
  mainWindow = new BrowserWindow({
    width: windowState.width,
    height: windowState.height,
    x: windowState.x,
    y: windowState.y,
    minWidth: 420,
    minHeight: 600,
    title: getBrand().displayName,
    icon: APP_ICON_PATH,
    webPreferences: {
      preload: getPreloadPath(__dirname),
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
    show: false,
    backgroundColor: '#ffffff',
  });
  if (windowState.isMaximized) mainWindow.maximize();

  const loadingPath = path.join(__dirname, '../pages/loading.html');
  void mainWindow.loadFile(loadingPath, { query: { brand: getBrand().displayName } });
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
    setTimeout(() => void loadApp(), 300);
  });

  const saveWindowState = () => {
    if (!mainWindow) return;
    const bounds = mainWindow.getBounds();
    store.set('windowState', {
      width: bounds.width,
      height: bounds.height,
      x: bounds.x,
      y: bounds.y,
      isMaximized: mainWindow.isMaximized(),
    });
  };
  mainWindow.on('resize', saveWindowState);
  mainWindow.on('move', saveWindowState);
  mainWindow.on('close', event => {
    saveWindowState();
    // 只有 macOS 保留"关窗口 = 隐藏到 Dock"的惯例。Windows / Linux 上没有
    // 托盘图标，隐藏窗口等于留下一个用户看不见、只能靠任务管理器杀掉的
    // 后台进程——这正是"关闭软件后有后台残留"的根因。非 macOS 直接退出。
    if (!isQuitting && process.platform === 'darwin') {
      event.preventDefault();
      mainWindow?.hide();
    }
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
    if (process.platform !== 'darwin') {
      app.quit();
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://') || url.startsWith('https://')) {
      void shell.openExternal(url);
    }
    return { action: 'deny' };
  });

  // Lock the window title. Without this, Electron auto-syncs from the page's
  // <title> (产品页可能带营销长标题). Calling preventDefault() keeps whatever
  // title we set on the BrowserWindow.
  mainWindow.on('page-title-updated', event => {
    event.preventDefault();
  });

  // Auto-open DevTools in dev mode so renderer issues are visible immediately.
  // Set DEEPPATH_DEVTOOLS=0 to suppress, or =1 in packaged builds to enable.
  const devtoolsFlag = process.env.DEEPPATH_DEVTOOLS;
  const shouldOpenDevtools = devtoolsFlag === '1' || (IS_DEV && devtoolsFlag !== '0');
  if (shouldOpenDevtools) {
    mainWindow.webContents.once('did-finish-load', () => {
      try {
        mainWindow?.webContents.openDevTools({ mode: 'detach' });
      } catch {
        // ignore — devtools are optional
      }
    });
  }

  createMenu();
}

/**
 * 宿主窗口能力（2.3）：为包打开一个加载应用内路由的独立窗口（如包
 * 自带的调试/日志窗）。同一时刻只保留一个独立路由窗口，重复打开聚焦
 * 既有窗口。复用 preload.js 暴露的 window.electron API。
 */
async function openRouteWindow(route: string, options: { title: string }): Promise<void> {
  if (routeWindow && !routeWindow.isDestroyed()) {
    routeWindow.show();
    routeWindow.focus();
    return;
  }
  routeWindow = new BrowserWindow({
    width: 920,
    height: 620,
    minWidth: 640,
    minHeight: 360,
    title: `${getBrand().displayName} · ${options.title}`,
    icon: APP_ICON_PATH,
    backgroundColor: '#0b0b0c',
    webPreferences: {
      preload: getPreloadPath(__dirname),
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
    show: false,
  });

  routeWindow.on('page-title-updated', (event) => {
    event.preventDefault();
  });
  routeWindow.on('closed', () => {
    routeWindow = null;
  });

  routeWindow.show();
  await loadWebRoute(routeWindow, route);

  if (process.env.DEEPPATH_DEVTOOLS === '1' || (IS_DEV && process.env.DEEPPATH_DEVTOOLS !== '0')) {
    try {
      routeWindow.webContents.openDevTools({ mode: 'detach' });
    } catch {
      /* ignore */
    }
  }
}

function createMenu(): void {
  const isMac = process.platform === 'darwin';
  const appName = app.getName();

  const template: Electron.MenuItemConstructorOptions[] = [
    // macOS 必须有 app 菜单（首位），否则 Cmd+H/Cmd+Q 等系统快捷键也会失效
    ...(isMac
      ? ([
          {
            label: appName,
            submenu: [
              { role: 'about', label: `关于 ${appName}` },
              { type: 'separator' },
              { role: 'services', label: '服务' },
              { type: 'separator' },
              { role: 'hide', label: `隐藏 ${appName}` },
              { role: 'hideOthers', label: '隐藏其他' },
              { role: 'unhide', label: '全部显示' },
              { type: 'separator' },
              { role: 'quit', label: `退出 ${appName}` },
            ],
          },
        ] as Electron.MenuItemConstructorOptions[])
      : []),
    {
      label: '文件',
      submenu: [
        {
          label: '新建对话',
          accelerator: 'CmdOrCtrl+N',
          click: () => mainWindow?.webContents.send('menu:new-chat'),
        },
        { type: 'separator' },
        isMac ? { role: 'close', label: '关闭窗口' } : { role: 'quit', label: '退出' },
      ],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        ...(isMac
          ? ([
              { role: 'pasteAndMatchStyle', label: '粘贴并匹配样式' },
              { role: 'delete', label: '删除' },
              { role: 'selectAll', label: '全选' },
              { type: 'separator' },
              {
                label: '语音',
                submenu: [
                  { role: 'startSpeaking', label: '开始朗读' },
                  { role: 'stopSpeaking', label: '停止朗读' },
                ],
              },
            ] as Electron.MenuItemConstructorOptions[])
          : ([
              { role: 'delete', label: '删除' },
              { type: 'separator' },
              { role: 'selectAll', label: '全选' },
            ] as Electron.MenuItemConstructorOptions[])),
      ],
    },
    {
      label: '视图',
      submenu: [
        {
          label: '打开终端',
          accelerator: 'CmdOrCtrl+T',
          click: () => {
            // 终端内嵌在主窗口的 /terminal 路由——通知 renderer 导航过去。
            if (mainWindow && !mainWindow.isDestroyed()) {
              mainWindow.show();
              mainWindow.focus();
              mainWindow.webContents.send('menu:open-terminal');
            }
          },
        },
        { type: 'separator' },
        { role: 'reload', label: '重新加载' },
        { role: 'forceReload', label: '强制重新加载' },
        { type: 'separator' },
        { role: 'toggleDevTools', label: '切换开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '切换全屏' },
      ],
    },
    ...(isMac
      ? ([
          {
            label: '窗口',
            submenu: [
              { role: 'minimize', label: '最小化' },
              { role: 'zoom', label: '缩放' },
              { type: 'separator' },
              { role: 'front', label: '全部置于顶层' },
            ],
          },
        ] as Electron.MenuItemConstructorOptions[])
      : []),
    // 帮助菜单链接是产品注入配置（3.1，product.json links）；中性 shell
    // 未注入时不渲染对应菜单项；两项都缺则整个「帮助」菜单不渲染。
    ...((): Electron.MenuItemConstructorOptions[] => {
      const links = getProductConfig().links ?? {};
      const submenu: Electron.MenuItemConstructorOptions[] = [];
      if (links.releasePage) {
        submenu.push({
          label: '打开发布页',
          click: async () => {
            await shell.openExternal(links.releasePage!);
          },
        });
      }
      if (links.website) {
        submenu.push({
          label: '访问官网',
          click: async () => {
            await shell.openExternal(links.website!);
          },
        });
      }
      return submenu.length > 0 ? [{ label: '帮助', submenu }] : [];
    })(),
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function setupIpcHandlers(): void {
  ipcMain.on('retry-connection', async () => {
    retryCount = 0;
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (IS_DEV) {
      const isUp = await checkDevServerConnection();
      if (isUp) await loadApp();
      else scheduleRetry();
    } else {
      await loadApp();
    }
  });

  ipcMain.handle('check-network-status', async () => {
    if (IS_DEV) return { online: await checkDevServerConnection() };
    return { online: true };
  });

  ipcMain.on('show-notification', (_event, payload: { title?: string; body?: string; link?: string }) => {
    const notification = new ElectronNotification({
      title: payload.title || getBrand().displayName,
      body: payload.body || '',
      silent: false,
    });
    notification.on('click', () => {
      if (!mainWindow) return;
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
      if (payload.link) {
        mainWindow.webContents.send('notification-clicked', { link: payload.link });
      }
    });
    notification.show();
  });

  // W4-1: the renderer's approval modal answers here. The decision is
  // validated and routed to the pending sidecar `approval.request` call;
  // unknown requestIds (late/stale answers) are dropped.
  ipcMain.handle('approval:decide', (_event, payload: unknown) =>
    approvalBridge.decide(payload),
  );
  ipcMain.handle('approval:pending', () => approvalBridge.pending());

  // W8: the renderer's ask-user card answers here; routed to the pending
  // sidecar `ask_user.request` call (fail-open empty answers on mismatch).
  ipcMain.handle('ask-user:answer', (_event, payload: unknown) =>
    askUserBridge.answer(payload),
  );
  ipcMain.handle('ask-user:pending', () => askUserBridge.pending());

  ipcMain.handle('local:update-safety-config', async (_event, config: CommandSafetyConfigPayload) => {
    localExecutor.updateSafetyConfig(config);
    return { success: true };
  });
  ipcMain.handle('local:select-directory', async (_event, args) => {
    if (!mainWindow) return { canceled: true, filePaths: [] };
    const options = (args as { title?: string } | undefined) ?? {};
    const result = await dialog.showOpenDialog(mainWindow, {
      title: options.title || '选择文件夹',
      properties: ['openDirectory'],
    });
    return result;
  });
  // 会话附件：把渲染进程给出的源路径（Electron）或 base64 字节（BS）落盘。
  ipcMain.handle(
    'attachments:save',
    async (
      _event,
      input: {
        chatId?: string;
        files?: Array<{ path?: string; name?: string; data?: string }>;
      },
    ) => {
      const chatId = typeof input?.chatId === 'string' ? input.chatId : '';
      const files = Array.isArray(input?.files)
        ? input.files.filter(
            (f) => f && (typeof f.path === 'string' || typeof f.data === 'string'),
          )
        : [];
      return saveAttachmentFiles(chatId, files);
    },
  );
  ipcMain.handle('local:save-text-file', async (_event, args: unknown) => {
    if (!mainWindow) return { canceled: true };
    const options = (args as { title?: string; defaultPath?: string; content?: string } | undefined) ?? {};
    const save = await dialog.showSaveDialog(mainWindow, {
      title: options.title || '导出文件',
      defaultPath: options.defaultPath || 'insights-export.json',
      filters: [{ name: 'JSON', extensions: ['json'] }],
    });
    if (save.canceled || !save.filePath) return { canceled: true };
    await fsPromises.writeFile(save.filePath, options.content ?? '', 'utf8');
    return { canceled: false, filePath: save.filePath };
  });
  // 分享对话截图：渲染层把 .chat-panel-container 的 getBoundingClientRect()
  // （CSS px = DIP）传上来，capturePage 按该区域截图并写入系统剪贴板。
  ipcMain.handle('local:capture-screenshot', async (event, args) => {
    try {
      const win = BrowserWindow.fromWebContents(event.sender);
      if (!win) return { success: false, error: 'window not found' };
      const rect = (args as { rect?: { x: number; y: number; width: number; height: number } } | undefined)?.rect;
      const image = rect
        ? await win.webContents.capturePage({
            x: Math.max(0, Math.round(rect.x)),
            y: Math.max(0, Math.round(rect.y)),
            width: Math.max(1, Math.round(rect.width)),
            height: Math.max(1, Math.round(rect.height)),
          })
        : await win.webContents.capturePage();
      if (image.isEmpty()) return { success: false, error: 'capturePage 返回空图像' };
      clipboard.writeImage(image);
      const size = image.getSize();
      return { success: true, width: size.width, height: size.height };
    } catch (err) {
      return { success: false, error: err instanceof Error ? err.message : String(err) };
    }
  });
  ipcMain.handle('local:exec-shell', async (_event, request: LocalExecRequest) => {
    const viaTerminal = await maybeExecInTerminal(request);
    if (viaTerminal) return viaTerminal;
    return localExecutor.executeShell(request);
  });
  ipcMain.handle('local:read-file', async (_event, request: LocalFileReadRequest) => localExecutor.readLocalFile(request));
  ipcMain.handle('local:write-file', async (_event, request: LocalFileWriteRequest) => localExecutor.writeLocalFile(request));
  ipcMain.handle('local:open-path', async (_event, request: LocalOpenRequest) => localExecutor.openLocalTarget(request));
  ipcMain.handle('local:list-scripts', async () => localScriptRegistry.list());
  ipcMain.handle('local:add-script', async (_event, input: CreateLocalScriptInput) => localScriptRegistry.create(input));
  ipcMain.handle('local:update-script', async (_event, payload: { id: string; updates: Partial<CreateLocalScriptInput> }) =>
    localScriptRegistry.update(payload.id, payload.updates)
  );
  ipcMain.handle('local:delete-script', async (_event, id: string) => {
    localScriptRegistry.delete(id);
    return { success: true };
  });
  ipcMain.handle('local:run-script', async (_event, id: string) => {
    const script = localScriptRegistry.getById(id);
    if (!script) return { success: false, error: `Script not found: ${id}` };
    return await localExecutor.executeShell({
      command: script.command,
      cwd: script.cwd,
      timeout: script.timeout,
    });
  });

  // ─── 包 IPC（2.3）：各包的通道贡献由装配产物给出，宿主循环注册；
  // 通道命名空间（<packId>:*）由 registerPackIpc 校验。开独立路由窗口
  // 是宿主窗口层能力，经 caps 注入给包。
  for (const [packId, handle] of packHandles) {
    if (handle.ipc) {
      registerPackIpc(packId, handle.ipc({ openRouteWindow }));
    }
  }

  ipcMain.handle('local-backend:request', async (_event, request: { method: string; path: string; body?: unknown }) => {
    try {
      const response = await localBackendRouter.handle(request);
      log.info(`[local-backend] ${request.method} ${request.path} -> ${response.status}`);
      return {
        ok: response.status < 400,
        status: response.status,
        data: response.data,
        error: response.status >= 400 ? JSON.stringify(response.data) : undefined,
      };
    } catch (error) {
      log.error(`[local-backend] ${request.method} ${request.path} threw`, error);
      return {
        ok: false,
        status: 500,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  });

  // 真正的流式 IPC：invoke 立刻返回一个 streamId，
  // 主进程在 router 每收到一个 SSE chunk 时通过 webContents.send 把它推到渲染端。
  // 渲染端 preload 把这些事件喂进 ReadableStream.controller。
  //
  // 取消机制：每个 stream 配一个 AbortController。以下任一情况触发 abort，
  // router 的 agent 循环会在下一个 LLM turn / tool call 边界停下来：
  //   1. renderer 主动调 cancelStream(streamId)（Stop 按钮 / 切换对话）
  //   2. 发起流的 webContents 被销毁（窗口关闭 / 页面刷新）
  //   3. 应用退出（before-quit 里统一 abort）
  // 没有这一层时，agent 循环会在用户关闭对话后继续跑完全部 32 轮，
  // 终端窗口被每条命令反复唤起——这正是"关了还在执行"的根因。
  ipcMain.handle(
    'local-backend:stream',
    async (event, request: { method: string; path: string; body?: unknown }) => {
      const streamId = randomUUID();
      const channel = `local-backend:stream:${streamId}`;
      const cancelChannel = `${channel}:cancel`;
      const controller = new AbortController();
      activeStreamControllers.set(streamId, controller);

      const onCancel = () => {
        log.info(`[local-backend stream] cancel requested`, { streamId });
        controller.abort();
      };
      ipcMain.once(cancelChannel, onCancel);

      const sender = event.sender;
      const onSenderDestroyed = () => {
        log.info(`[local-backend stream] sender destroyed, aborting`, { streamId });
        controller.abort();
      };
      sender.once('destroyed', onSenderDestroyed);

      const cleanup = () => {
        activeStreamControllers.delete(streamId);
        ipcMain.removeListener(cancelChannel, onCancel);
        if (!sender.isDestroyed()) sender.removeListener('destroyed', onSenderDestroyed);
      };

      // 异步启动 router，避免阻塞 invoke 返回（前端拿到 streamId 才能开始监听）。
      queueMicrotask(async () => {
        const safeSend = (payload: unknown) => {
          if (!sender.isDestroyed()) sender.send(channel, payload);
        };
        try {
          const result = await localBackendRouter.handleStream(
            request,
            chunk => {
              safeSend({ type: 'data', chunk });
            },
            { signal: controller.signal }
          );
          log.info(
            `[local-backend stream] ${request.method} ${request.path} -> ${result.status}`
          );
          safeSend({ type: 'end', status: result.status });
        } catch (err) {
          log.error(
            `[local-backend stream] ${request.method} ${request.path} threw`,
            err
          );
          safeSend({
            type: 'error',
            error: err instanceof Error ? err.message : String(err),
          });
        } finally {
          cleanup();
        }
      });

      return { ok: true, streamId };
    }
  );

  // 轮中转向：把用户消息注入正在运行的 CoreLoop 回合（sidecar 侧 drain 进
  // transcript）。仅 CoreLoop 路径支持；无活动回合时软失败 { ok: false }，
  // 渲染端保留草稿，等回合结束后按普通消息重发。
  ipcMain.handle(
    'local-backend:steer',
    async (_event, payload: { chatId?: string; content?: string }) => {
      const chatId = typeof payload?.chatId === 'string' ? payload.chatId : '';
      const content = typeof payload?.content === 'string' ? payload.content : '';
      if (!chatId || !content.trim()) {
        return { ok: false, reason: 'invalid_params' };
      }
      const streamId = getActiveCoreLoopStreamId(chatId);
      const supervisor = getSidecarSupervisor();
      if (!streamId || !supervisor) {
        return { ok: false, reason: 'no_active_coreloop_turn' };
      }
      const ok = await supervisor.steerChat(streamId, content);
      if (ok) {
        // The framework's record logs the injected message (the loop drains it
        // into the transcript), but the desktop store is what the chat renders
        // on reopen — without this the user comes back to an answer whose
        // question is missing. Written at steer time, so `created_at` places it
        // between the turn's user message and the assistant reply persisted at
        // turn end. Only on acceptance: a soft failure degrades into the
        // follow-up queue, which persists through the normal send path.
        localStore.addMessage(chatId, 'user', content);
      }
      return { ok, ...(ok ? {} : { reason: 'stream_not_active' }) };
    }
  );

  // ---------------------------------------------------------------------------
  // Terminal IPC: visible PTY embedded in the main window's /terminal route
  // ---------------------------------------------------------------------------
  ipcMain.handle('terminal:list', async () => terminalManager.list());
  ipcMain.handle('terminal:spawn', async (_event, options: TerminalSpawnOptions = {}) => {
    return terminalManager.spawn(options);
  });
  ipcMain.handle('terminal:ensure', async (event, options: TerminalSpawnOptions = {}) => {
    const session = terminalManager.ensurePrimary(options);
    // Replay any output buffered before the renderer was attached, so the
    // window doesn't look empty if the agent kicked off a command before the
    // user had the terminal open. Send to the requesting webContents directly,
    // not via broadcast, to avoid double-writing to other listeners.
    const replay = terminalManager.getReplayBuffer(session.id);
    if (replay) {
      try {
        event.sender.send('terminal:data', { sessionId: session.id, chunk: replay });
      } catch {
        // ignore — sender may have gone away
      }
    }
    return session;
  });
  ipcMain.handle('terminal:write', async (_event, payload: { id: string; data: string }) => {
    return terminalManager.write(payload.id, payload.data);
  });
  ipcMain.handle(
    'terminal:resize',
    async (_event, payload: { id: string; cols: number; rows: number }) => {
      return terminalManager.resize(payload.id, payload.cols, payload.rows);
    }
  );
  ipcMain.handle('terminal:kill', async (_event, id: string) => terminalManager.kill(id));
  ipcMain.handle(
    'terminal:exec',
    async (_event, payload: { id?: string; command: string; timeoutMs?: number }) => {
      let id = payload.id;
      if (!id) {
        const session = terminalManager.ensurePrimary();
        id = session.id;
      }
      try {
        return await terminalManager.exec(id, payload.command, payload.timeoutMs);
      } catch (err) {
        return {
          success: false,
          exitCode: -1,
          stdout: '',
          stderr: err instanceof Error ? err.message : String(err),
          truncated: false,
          durationMs: 0,
        };
      }
    }
  );
}

app.whenReady().then(async () => {
  applyStrictCsp();
  setupIpcHandlers();
  // 任务清扫 / mock 广播 / sidecar 启动 / MCP 刷新 / 终端预热都在
  // runtime.start() 里（与 BS 同一份）。
  runtime.start();
  createWindow();
  setSecondInstanceHandler(() => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
  app.on('activate', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.show();
      mainWindow.focus();
    } else {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  // macOS 惯例：所有窗口关闭后应用驻留 Dock。其他平台直接退出，
  // 避免无窗口的后台残留进程。
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  if (retryTimer) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  // 中止所有仍在跑的 agent 流，防止退出后 LLM 循环 / 工具调用继续执行。
  for (const controller of activeStreamControllers.values()) {
    controller.abort();
  }
  activeStreamControllers.clear();
  // 终端 / 场景包进程 / sidecar 的关停统一走 runtime。
  void runtime.shutdown();
});

nativeTheme.on('updated', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('theme-changed', nativeTheme.shouldUseDarkColors);
  }
});
