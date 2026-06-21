export * from './sidecar/index.js';
export * from './bridge/types.js';
export { LocalExecutor } from './local/local-executor.js';
export { LocalToolRouter, ToolSpec } from './local/tool-router.js';
export { TerminalManager } from './terminal/terminal-manager.js';
export { registerDesktopIpcHandlers, DesktopIpcConfig } from './app/factory.js';
