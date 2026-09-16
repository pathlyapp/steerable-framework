/**
 * 宿主日志单例出口：electron-log 的 main 模块在 import 时注册
 * `__ELECTRON_LOG__` IPC handler——同一个 Electron 进程里只能有一个
 * 物理副本完成注册。shell 与场景包若各自从所属仓库的 node_modules
 * 解析 electron-log（link: 依赖下是两个物理路径），第二个 import
 * 会抛 "Attempted to register a second handler"。因此 electron-log
 * 只由 shell 直接依赖，场景包/产品代码一律经本模块拿同一个实例：
 *
 *   import { log } from '@steerable/agent-shell/log';
 */
import log from 'electron-log';

export { log };
export type { Logger, LogMessage, LogLevel } from 'electron-log';
