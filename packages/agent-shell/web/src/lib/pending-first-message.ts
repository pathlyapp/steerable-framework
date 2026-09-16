/**
 * 首页输入框 → 新对话首条消息的接力棒。
 *
 * EmptyChatGate（无 chatId 的落地页）的输入框提交时，流程是
 * "先 createChat → 再 navigate → 新 chat 视图挂载后才发得出消息"。
 * 但 submit 时拿到的文本在 navigate 之后就离开了作用域——react-router 的
 * location.state 能跨跳转，可 hash 路由 + HMR 下它不够稳（刷新即丢，
 * 且 AgentChatLoader 的 key 重挂载语义会让读取时机变得微妙）。
 *
 * 这里用模块级单例暂存：写入方（EmptyChatGate）在拿到 chatId 后、跳转前
 * set；消费方（AgentChatView）挂载后 take 一次并自动发送。take 按
 * chatId 匹配且取出即清，StrictMode 双跑 effect / 重复挂载都不会重发。
 * 页面刷新后暂存丢失——可接受：刷新发生在流式开始前的话，chat 已经建好，
 * 用户重发一次即可。
 */

export interface PendingFirstMessage {
  chatId: string;
  content: string;
  metadata?: Record<string, unknown>;
}

let pending: PendingFirstMessage | null = null;

export function setPendingFirstMessage(msg: PendingFirstMessage): void {
  pending = msg;
}

export function takePendingFirstMessage(
  chatId: string,
): PendingFirstMessage | null {
  if (!pending || pending.chatId !== chatId) return null;
  const msg = pending;
  pending = null;
  return msg;
}
