import { ipcMain } from 'electron';
import type { TerminalManager } from '../terminal/terminal-manager.js';
import type { LocalExecutor } from '../local/local-executor.js';
import type { SidecarSupervisor } from '../sidecar/supervisor.js';

export interface DesktopIpcConfig {
  terminalManager: TerminalManager;
  localExecutor: LocalExecutor;
  sidecarSupervisor?: SidecarSupervisor;
}

/**
 * Registers standard, generic Steerable IPC handlers on Electron's ipcMain.
 * Bridges the Electron renderer's preload calls (ElectronBridge) directly to the core
 * desktop managers.
 */
export function registerDesktopIpcHandlers(config: DesktopIpcConfig): void {
  const { terminalManager, localExecutor, sidecarSupervisor } = config;

  // 1. Core local execution handlers
  ipcMain.handle('local:exec-shell', async (_event, payload) => {
    return await localExecutor.executeShell({
      command: payload.command,
      cwd: payload.cwd,
      timeoutMs: payload.timeoutMs,
    });
  });

  ipcMain.handle('local:read-file', async (_event, payload) => {
    return await localExecutor.readLocalFile(payload);
  });

  ipcMain.handle('local:write-file', async (_event, payload) => {
    return await localExecutor.writeLocalFile(payload);
  });

  ipcMain.handle('local:open-path', async (_event, payload) => {
    return await localExecutor.openLocalTarget(payload);
  });

  ipcMain.handle('local:update-safety-config', async (_event, payload) => {
    localExecutor.updateSafetyConfig(payload);
    return { success: true };
  });

  // 2. Terminal session handlers
  ipcMain.handle('terminal:list', async () => {
    return terminalManager.list();
  });

  ipcMain.handle('terminal:spawn', async (_event, payload) => {
    return terminalManager.spawn(payload);
  });

  ipcMain.handle('terminal:ensure', async (_event, payload) => {
    return terminalManager.ensurePrimary(payload);
  });

  ipcMain.handle('terminal:write', async (_event, payload) => {
    return terminalManager.write(payload.id, payload.data);
  });

  ipcMain.handle('terminal:resize', async (_event, payload) => {
    return terminalManager.resize(payload.id, payload.cols, payload.rows);
  });

  ipcMain.handle('terminal:kill', async (_event, payload) => {
    return terminalManager.kill(payload);
  });

  ipcMain.handle('terminal:exec', async (_event, payload) => {
    return await terminalManager.exec(payload.id, payload.command, payload.timeoutMs);
  });

  // 3. Optional Sidecar JSON-RPC backend bridge
  if (sidecarSupervisor) {
    ipcMain.handle('local-backend:request', async (_event, payload: { method: string; path: string; body?: unknown }) => {
      // Bridges generic REST requests to sidecar JSON-RPC calls
      try {
        const result = await sidecarSupervisor.call(payload.method, payload.body);
        return { ok: true, status: 200, data: result };
      } catch (err: any) {
        return { ok: false, status: 500, error: err.message || 'Sidecar call failed' };
      }
    });
  }
}
