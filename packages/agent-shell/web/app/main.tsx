// agent-shell 中性独立入口：不注册任何场景包，直接 bootstrap——
// 品牌/配置全部落 shell 中性默认（Steerable Shell）。供框架仓用户
// 一键体验 shell 本体（框架根 pnpm agent-shell:web / agent-shell:client）。
import { bootstrap } from '@/main';

void bootstrap();
