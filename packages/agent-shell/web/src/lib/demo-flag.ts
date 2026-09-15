/**
 * Demo-mode flag for the static website build (steerableframework.com/demo/).
 * The app-demo entry calls markDemoMode() before bootstrap; AppShell reads it
 * for the demo banner and the browser mock reads it to allow installation
 * outside DEV builds. Normal app builds never call markDemoMode().
 */
let demoMode = false;

export function markDemoMode(): void {
  demoMode = true;
}

export function isDemoMode(): boolean {
  return demoMode;
}
