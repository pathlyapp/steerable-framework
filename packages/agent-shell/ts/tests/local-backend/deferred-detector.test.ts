import { describe, it, expect } from 'vitest';
import { detectDeferredExecution } from '../../src/local-backend/deferred-detector.js';

describe('detectDeferredExecution', () => {
  describe('真实回归 case', () => {
    it('cflog "现在轮询结果。" 必须被识别为 deferred（句号结尾也算）', () => {
      // 这是 t_mp9vqnv7_1 那次抓到的真实文本——模型 emit 完 cflog_replay_card
      // 拿到 pending 状态后，写了这一段陈述就停笔。
      const text =
        '任务已排队，任务 ID 为 t_mp9vqnv7_1。现在轮询结果。';
      expect(detectDeferredExecution(text)).toBe(true);
    });

    it('"卡片已触发回放，任务 ID 为 task_xxx。正在等待结果..." 也算 deferred', () => {
      const text =
        '卡片已触发回放，任务 ID 为 task_demo_001。正在等待结果...';
      expect(detectDeferredExecution(text)).toBe(true);
    });

    it('"现在执行卡片..." 历史 case 不能漏', () => {
      expect(detectDeferredExecution('好的，现在执行卡片...')).toBe(true);
    });

    it('英文 "I\'ll now poll for the result." 也算 deferred', () => {
      expect(
        detectDeferredExecution("Task queued. I'll now poll for the result."),
      ).toBe(true);
    });
  });

  describe('意图词 + 开放收尾（rule ①/②）', () => {
    it('意图词 + 省略号 → deferred', () => {
      expect(detectDeferredExecution('我马上调用 cflog_get_task_result...')).toBe(true);
    });

    it('意图词 + 冒号收尾 → deferred', () => {
      expect(
        detectDeferredExecution('好的，接下来调用 cflog_replay_card 工具：'),
      ).toBe(true);
    });

    it('"现在 X" + 中文省略号 → deferred', () => {
      expect(detectDeferredExecution('现在搜索相关卡片……')).toBe(true);
    });
  });

  describe('避免误伤（已完成 / 总结类）', () => {
    it('过去时陈述 "我已经把任务列出来了。" 不应误判', () => {
      // 旧规则曾允许 "我" 单独出现做主语，配合 rule ③ 会把这句误判。
      // 现在 "我" 必须带未来时态修饰词。
      expect(detectDeferredExecution('我已经把任务列出来了。')).toBe(false);
    });

    it('客观陈述 "卡片执行完成，输出曲线 GR_SHIFTED。" 不应误判', () => {
      expect(
        detectDeferredExecution(
          '卡片执行完成，输出曲线 GR_SHIFTED 已写回工作区。',
        ),
      ).toBe(false);
    });

    it('总结句 "结果如下" 不应误判（没意图主语）', () => {
      expect(detectDeferredExecution('找到 2 个卡片，结果如下：A、B。')).toBe(false);
    });

    it('过渡句在段落中间但最后一句是完成态 → 不算 deferred', () => {
      // "先 X，然后 Y" 这种过渡句出现在中间是合法的；只要**最后一句**
      // 是完成态结论就放行。
      const text =
        '先调用了 cflog_test_connection，然后调用了 cflog_list_cards，已找到 2 个相关卡片。';
      expect(detectDeferredExecution(text)).toBe(false);
    });
  });

  describe('澄清 / 条件式承诺收尾不误报（2026-07-05 预算烧爆回归）', () => {
    // 真实日志 case：模型向用户征询输入并承诺"拿到信息后执行"，
    // 旧规则 ③ 命中"我来…执行"强制重试两次，直接烧爆 token 预算。
    it('"告诉我你现在想做什么，我来帮你执行。" → 不算 deferred', () => {
      expect(
        detectDeferredExecution(
          '目前没有已注册的脚本记录。\n- 或者直接告诉我你现在想做什么，我来帮你执行。',
        ),
      ).toBe(false);
    });

    it('"告诉我文件路径和需求，我立刻执行。" → 不算 deferred', () => {
      expect(
        detectDeferredExecution('告诉我文件路径和需求，我立刻执行。'),
      ).toBe(false);
    });

    it('"如果你需要，我马上运行。" → 不算 deferred', () => {
      expect(detectDeferredExecution('如果你需要，我马上运行。')).toBe(false);
    });

    it('条件从句在动词之后不豁免："我现在就执行，如果失败会重试。"', () => {
      expect(
        detectDeferredExecution('我现在就执行，如果失败会重试。'),
      ).toBe(true);
    });
  });

  describe('boundary', () => {
    it('空字符串 / 太短文本不触发', () => {
      expect(detectDeferredExecution('')).toBe(false);
      expect(detectDeferredExecution('好的')).toBe(false);
      expect(detectDeferredExecution('   ')).toBe(false);
    });

    it('单纯长省略号收尾且文本短 → 兜底触发（rule ④）', () => {
      expect(detectDeferredExecution('好的，先检查一下后台情况……')).toBe(true);
    });

    it('长篇分析末尾自然省略号不应触发（rule ④ 字数限制）', () => {
      const longTail = '稳定状态'.repeat(120); // ~ 480 字符
      expect(detectDeferredExecution(`${longTail}……`)).toBe(false);
    });
  });
});
