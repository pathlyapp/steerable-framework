/**
 * chat-title 的 `[自动化] ` 前缀解析：本地后端与云端后端共用同一前缀约定，
 * 解析器必须同时接受「带空格前缀」与「裸括号」两种写法，且只有裸括号写法
 * 会 trimStart——带空格分支保留剩余前导空白（此处逐字锁定这一不对称，
 * 改动它属于行为变更）。
 */
import { describe, expect, it } from 'vitest';
import { AUTOMATION_TITLE_PREFIX, parseChatTitle } from './chat-title';

describe('parseChatTitle', () => {
  it('null / undefined / 空串 → 空标题且非自动化', () => {
    for (const raw of [null, undefined, '']) {
      expect(parseChatTitle(raw)).toEqual({ displayTitle: '', isAutomation: false });
    }
  });

  it('带空格前缀 → 去掉前缀、标记自动化', () => {
    expect(parseChatTitle(`${AUTOMATION_TITLE_PREFIX}每日报表`)).toEqual({
      displayTitle: '每日报表',
      isAutomation: true,
    });
  });

  it('裸括号（无空格）→ 去掉括号并 trimStart', () => {
    expect(parseChatTitle('[自动化]每日报表')).toEqual({
      displayTitle: '每日报表',
      isAutomation: true,
    });
    expect(parseChatTitle('[自动化]  每日报表')).toEqual({
      displayTitle: ' 每日报表',
      isAutomation: true,
    });
  });

  it('仅剩前缀本身 → 空标题但仍标记自动化', () => {
    expect(parseChatTitle('[自动化] ')).toEqual({ displayTitle: '', isAutomation: true });
    expect(parseChatTitle('[自动化]')).toEqual({ displayTitle: '', isAutomation: true });
  });

  it('普通标题原样透传', () => {
    expect(parseChatTitle('聊聊架构')).toEqual({
      displayTitle: '聊聊架构',
      isAutomation: false,
    });
  });

  it('前缀出现在中段不算自动化', () => {
    expect(parseChatTitle('笔记：[自动化] 方案')).toEqual({
      displayTitle: '笔记：[自动化] 方案',
      isAutomation: false,
    });
  });
});
