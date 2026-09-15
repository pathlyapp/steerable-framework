/**
 * JSON 键值存储工厂（agent-mcp-servers.json / agent-projects.json / window-state）。
 *
 * 历史上用 electron-store；它只在构造时读 `app.getPath('userData')` 当 cwd，
 * 而 conf（electron-store 的底层，已是本项目直接依赖）API 完全覆盖用到的
 * get/set + name + defaults 子集。统一走 conf + getUserDataDir()，Electron
 * 与 BS server 得到同一个文件路径，两种模式共享同一份数据。
 *
 * conf@10 的 d.ts 是 ESM 风格但包本身没有 `type: module`，NodeNext 下类型
 * 解析会塌成 namespace——所以这里声明用到的最小接口并做一次强转，不依赖
 * 它的声明文件。
 */
import ConfImport from 'conf';
import { getUserDataDir } from './runtime.js';

export interface JsonStore<T extends Record<string, unknown>> {
  get<Key extends keyof T>(key: Key): T[Key];
  get<Key extends keyof T>(key: Key, defaultValue: Required<T>[Key]): Required<T>[Key];
  set<Key extends keyof T>(key: Key, value?: T[Key]): void;
}

type ConfCtor = new <T extends Record<string, unknown>>(options: {
  cwd: string;
  name: string;
  defaults?: Partial<T>;
}) => JsonStore<T>;

const Conf = ConfImport as unknown as ConfCtor;

export function createJsonStore<T extends Record<string, unknown>>(options: {
  name: string;
  defaults?: Partial<T>;
}): JsonStore<T> {
  return new Conf<T>({
    cwd: getUserDataDir(),
    name: options.name,
    defaults: options.defaults,
  });
}
