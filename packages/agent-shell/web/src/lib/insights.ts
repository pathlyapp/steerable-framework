import { getElectronBridge } from './electron-bridge';

/** Fire-and-forget local enqueue. Never throws into UI. */
export function trackBehavior(
  eventName: string,
  properties: Record<string, unknown> = {},
): void {
  const bridge = getElectronBridge();
  if (!bridge) return;
  void bridge.localBackend
    .request({
      method: 'POST',
      path: '/api/v2/insights/events',
      body: { eventName, properties },
    })
    .catch(() => {});
}
