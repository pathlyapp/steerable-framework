/** Failed to spawn or hand-shake the sidecar. */
export class SidecarBootError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = 'SidecarBootError';
  }
}

/** A JSON-RPC method call returned an error frame. */
export class SidecarMethodError extends Error {
  constructor(
    message: string,
    public readonly code: number,
    public readonly kind: string | undefined,
    public readonly data: unknown,
  ) {
    super(message);
    this.name = 'SidecarMethodError';
  }
}

/** Sidecar exited or stopped responding before a request completed. */
export class SidecarShutdownError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SidecarShutdownError';
  }
}
