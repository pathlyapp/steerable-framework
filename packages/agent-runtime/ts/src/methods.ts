/**
 * The sidecar JSON-RPC method surface this runtime covers, as data.
 *
 * This list is the TS side of the 3.2.4 conformance gate:
 * `test/surface.test.ts` parses the `register("<method>", …)` lines out of
 * the Python sidecar (`packages/sidecar/py/src/steerable_sidecar/sidecar.py`)
 * and asserts the two sets are identical — so a method added to the sidecar
 * without a runtime wrapper (or a wrapper for a method the sidecar no longer
 * has) fails CI instead of drifting silently.
 */

/** Every host→sidecar request method the sidecar registers. */
export const SIDECAR_METHODS = [
  'system.ping',
  'system.shutdown',
  'system.shutdown_now',
  'agent.session.create',
  'agent.session.resume',
  'agent.session.list',
  'agent.session.fork',
  'agent.session.branches',
  'agent.session.messages',
  'agent.chat.stream',
  'agent.chat.cancel',
  'agent.chat.steer',
  'agent.chat.fork',
  'tool.list',
  'tool.invoke',
  'workspace.apply_edits',
  'skills.list',
  'trace.fetch',
  'trace.export',
  'config.get',
  'config.set',
  'compat.describe',
] as const;

export type SidecarMethod = (typeof SIDECAR_METHODS)[number];

/** Notification methods the sidecar pushes (no id, no response). */
export const SIDECAR_NOTIFICATIONS = [
  'lifecycle.ready',
  'lifecycle.shutdown',
  'stream.chunk',
  'stream.done',
  'stream.error',
  'agent.child',
] as const;

export type SidecarNotification = (typeof SIDECAR_NOTIFICATIONS)[number];
