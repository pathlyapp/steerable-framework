/**
 * Electron single-instance lock. Imported first from main.ts so a second
 * OS process exits before LocalStore opens the product DB.
 *
 * `requestSingleInstanceLock` must run before `app.whenReady`. `process.exit`
 * is required because `app.quit()` is async and later imports would still
 * construct LocalStore against the same userData.
 */

import { app } from 'electron';

const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
  process.exit(0);
}

type SecondInstanceHandler = () => void;
let secondInstanceHandler: SecondInstanceHandler | null = null;

export function setSecondInstanceHandler(handler: SecondInstanceHandler): void {
  secondInstanceHandler = handler;
}

app.on('second-instance', () => {
  secondInstanceHandler?.();
});
