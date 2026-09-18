import type { ComponentType } from 'react';

/**
 * Optional product-provided gate rendered before the application router.
 */
export interface AppShellGate {
  /** Allows one product entry to disable its gate for unsupported runtimes. */
  enabled(): boolean;
  /** Renders authentication UI and resumes bootstrap through the callback. */
  Component: ComponentType<{ onAuthenticated: () => void }>;
}

let registeredGate: AppShellGate | null = null;

/** Registers the app-shell gate before calling `bootstrap()`. */
export function registerAppShellGate(gate: AppShellGate): void {
  if (registeredGate) {
    throw new Error('[app-shell-gate] a gate is already registered');
  }
  registeredGate = gate;
}

/** Returns the product-provided gate, if one was registered. */
export function getAppShellGate(): AppShellGate | null {
  return registeredGate;
}
