/**
 * BS 模式的 SSE 事件总线：替代 Electron 的 `webContents.send(channel, payload)`
 * 广播。所有宿主事件（terminal:data、approval:request、chat-title-updated、
 * 场景包事件……）经这一条 `GET /api/v2/events` 长连接推给浏览器，
 * channel 名与 IPC 通道保持一致，前端 http-bridge 按名分发。
 */
import type { ServerResponse } from 'node:http';

export class SseBus {
  private readonly clients = new Set<ServerResponse>();

  /** 已连接浏览器数；approval 桥用它判断"有没有人能应答"（对齐 hasWindow）。 */
  get size(): number {
    return this.clients.size;
  }

  attach(res: ServerResponse): void {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.write(': connected\n\n');
    this.clients.add(res);
    res.on('close', () => this.clients.delete(res));
  }

  broadcast(channel: string, payload: unknown): void {
    if (this.clients.size === 0) return;
    const frame = `event: ${channel}\ndata: ${JSON.stringify(payload ?? null)}\n\n`;
    for (const res of this.clients) {
      try {
        res.write(frame);
      } catch {
        this.clients.delete(res);
      }
    }
  }
}
