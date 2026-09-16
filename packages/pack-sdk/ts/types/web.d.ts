/**
 * 渲染层包贡献类型（ScenarioPack.renderer 的 web 侧契约）。
 *
 * 注册表实现住在宿主 apps/web/src/packs/registry.ts（re-export 本模块类型
 * 兼容既有调用方）；包的 web 代码只应依赖本模块，不反向 import 宿主实现。
 */

import type { ComponentType } from 'react';

export interface PackRouteContribution {
  /** hash 路由路径（不带前导斜杠），如 '<pack>-debug-log'。 */
  readonly path: string;
  readonly Component: ComponentType;
}

export interface PackSettingsPanelContribution {
  readonly panelId: string;
  readonly title: string;
  /** 段头图标（react-icons 组件）；缺省段头只显示标题文字。 */
  readonly Icon?: ComponentType<{ className?: string }>;
  readonly Component: ComponentType;
}

/** 聊天页右侧栏位面板的标准 props（shell 布局注入）。 */
export interface PackChatSlotProps {
  /** 当前打开的会话 id（无会话时为空串）。 */
  chatId: string;
  /** 关闭栏位（用户点面板右上角 ×）。 */
  onClose: () => void;
  /**
   * 把一段内容作为普通用户消息发进当前会话（包的 fallback 逃生通道，
   * 如文档包的单页修改在 sidecar 未就绪时退回主聊天发送）。未注册时返回 false。
   */
  onSubmitToChat: (input: {
    content: string;
    metadata?: Record<string, unknown>;
  }) => boolean | void | Promise<boolean | void>;
}

/** 自动展开钩子拿到的布局 API。 */
export interface PackChatSlotAutoRevealApi {
  /** 请求把右侧栏位切到本槽位（栏位被占用时布局忽略——互斥规则在布局）。 */
  reveal: () => void;
  /** 当前会话 id（事件载荷据此过滤）。 */
  getCurrentChatId: () => string | null;
}

export interface PackChatSlotContribution {
  readonly slotId: string;
  /** 侧栏分段控件上的短名（如 'PPT 预览'）。 */
  readonly title: string;
  /** 分段控件图标（react-icons 组件）。 */
  readonly Icon: ComponentType<{ className?: string }>;
  /** 面板本体；shell 布局注入 {@link PackChatSlotProps}。 */
  readonly Component: ComponentType<PackChatSlotProps>;
  /**
   * 自动展开：布局挂载时调用一次，包实现自己的事件订阅（桥接事件），
   * 命中当前会话时调 reveal()。返回 cleanup（布局卸载时调用）。
   */
  readonly setupAutoReveal?: (api: PackChatSlotAutoRevealApi) => (() => void) | void;
}

export interface PackRendererContributions {
  readonly routes?: readonly PackRouteContribution[];
  readonly settingsPanels?: readonly PackSettingsPanelContribution[];
  readonly chatSlots?: readonly PackChatSlotContribution[];
  /**
   * 包技能里由引擎自动注入、不进 "/" 菜单的技能名/目录名
   * （并入 slash-sources 的隐藏集，如某包的 90-<pack>）。
   */
  readonly hiddenSlashSkills?: readonly string[];
}
