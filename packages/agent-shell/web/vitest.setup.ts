/**
 * 测试环境的 localStorage 兜底。
 *
 * Node ≥22.4 的 globalThis 自带实验性 localStorage（未传 --localstorage-file
 * 时访问只得 undefined），vitest 的 happy-dom 环境因此不再把 happy-dom
 * 自己的实现拷进全局——测试里 localStorage / window.localStorage 双双缺失，
 * 而产品代码（如落地页、exec-policy）按浏览器语义假定它存在。
 *
 * 这里在缺失时装一个内存 Storage 实现，让套件与宿主机 Node 版本解耦。
 * setupFiles 按测试文件执行（isolate 默认开启），每个文件拿到全新存储。
 */

class MemoryStorage {
  private readonly store = new Map<string, string>();

  get length(): number {
    return this.store.size;
  }

  clear(): void {
    this.store.clear();
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }

  key(index: number): string | null {
    return [...this.store.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

function installLocalStorage(): void {
  const storage = new MemoryStorage();
  const targets: object[] =
    typeof window !== 'undefined' ? [globalThis, window] : [globalThis];
  for (const target of targets) {
    // 读取一次触发 Node 实验性 getter 是安全的（仅打印一次警告）；
    // 已有可用实现的环境（如旧 Node 下 happy-dom 正常注入）不覆盖。
    if ((target as { localStorage?: unknown }).localStorage) continue;
    try {
      Object.defineProperty(target, 'localStorage', {
        value: storage,
        configurable: true,
        writable: true,
      });
    } catch {
      // 极端环境（getter 不可配置）下保持原样；exec-policy 的 SSR 护栏
      // 用例覆盖「无 localStorage」路径。
    }
  }
}

installLocalStorage();
