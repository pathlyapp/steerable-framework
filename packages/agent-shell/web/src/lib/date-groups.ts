/**
 * Date-group helpers used by AgentSidebar to label chat groups
 * ("今天" / "昨天" / "N天前" / "M月D日" / "YYYY年M月D日"). Lifted verbatim
 * from `deeppath/apps/web/src/app/agent/AgentSidebar.tsx` so the visual
 * grouping stays in lockstep.
 *
 * priority is used for ordering groups: smaller = newer = listed first.
 */

export function getDateGroupLabel(date: Date): string {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const target = new Date(date);
  target.setHours(0, 0, 0, 0);
  const diffDays =
    (today.getTime() - target.getTime()) / (1000 * 60 * 60 * 24);
  if (diffDays === 0) return '今天';
  if (diffDays === 1) return '昨天';
  if (diffDays < 4) return `${Math.floor(diffDays)}天前`;
  const m = target.getMonth() + 1;
  const d = target.getDate();
  if (target.getFullYear() !== today.getFullYear()) {
    return `${target.getFullYear()}年${m}月${d}日`;
  }
  return `${m}月${d}日`;
}

export function getDateGroupPriority(label: string): number {
  if (label === '今天') return 1;
  if (label === '昨天') return 2;
  if (label.endsWith('天前')) return 2 + parseInt(label, 10);
  if (label.includes('年')) return 1000;
  return 100;
}
