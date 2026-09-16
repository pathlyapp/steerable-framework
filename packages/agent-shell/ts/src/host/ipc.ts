/**
 * 包 IPC 绑定器（0.3e）：把包的 IpcContribution 注册到 Electron ipcMain。
 *
 * 命名空间规则：channel 必须以 `<packId>:` 开头——包只能占用自己的
 * 命名空间，注册期 fail fast。handler 统一收单个序列化载荷（renderer
 * 经 preload 透传），返回值经 IPC 结构化克隆回 renderer。
 */

import { ipcMain } from 'electron';

import type { IpcContribution } from '../scenario/pack.js';

/** 命名空间校验（纯函数，单测直接覆盖）。 */
export function assertPackChannel(packId: string, channel: string): void {
  if (!channel.startsWith(`${packId}:`)) {
    throw new Error(
      `[pack-ipc] channel "${channel}" must be namespaced under "${packId}:"`,
    );
  }
}

export function registerPackIpc(
  packId: string,
  contributions: readonly IpcContribution[],
): void {
  for (const contribution of contributions) {
    assertPackChannel(packId, contribution.channel);
    ipcMain.handle(contribution.channel, (_event, payload) => contribution.handler(payload));
  }
}
