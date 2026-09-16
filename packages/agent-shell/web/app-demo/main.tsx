// 官网静态 demo 入口（steerableframework.com/demo/）：Pages 上没有后端，
// 先打 demo 标记并安装浏览器 mock（fixture 数据 + 模拟流式回复），
// 再走标准 bootstrap——用户看到的是真实 shell UI 在 mock 数据上运行。
import { markDemoMode } from '@/lib/demo-flag';
import { installBrowserDevElectronMock } from '@/lib/browser-dev-electron-mock';
import { bootstrap } from '@/main';

markDemoMode();
installBrowserDevElectronMock();
void bootstrap();
