import { StoreAlreadyOwnedError } from './write-lease.js';

interface ElectronExit {
  app: { exit(code: number): void };
  dialog: { showErrorBox(title: string, message: string): void };
}

/** Maps write-lease contention to the desktop's user-facing startup failure. */
export function acquireWriteLeaseOrExit<T>(
  acquire: () => T,
  loadElectron: () => Promise<ElectronExit> = () =>
    import('electron') as unknown as Promise<ElectronExit>,
  exitProcess: (code: number) => void = (code) => process.exit(code),
): T {
  try {
    return acquire();
  } catch (error) {
    if (error instanceof StoreAlreadyOwnedError) {
      void loadElectron()
        .then(({ app, dialog }) => {
          dialog.showErrorBox(
            '无法启动',
            '另一个实例正在运行并占用本地数据库。请先关闭该实例，再重新打开应用。',
          );
          app.exit(1);
        })
        .catch(() => exitProcess(1));
    }
    throw error;
  }
}
