/**
 * Detects "deferred-execution hallucination" — the failure mode where the LLM
 * narrates an upcoming action (e.g. "现在轮询结果。" / "I'll now poll…") but
 * does NOT actually emit the corresponding tool_call, leaving the stream
 * dangling.
 *
 * The in-turn retry machinery that used to act on this signal moved to the
 * sidecar's Python AntiHallucinationHooks (2026-08-26, TS loop deleted). The
 * surviving caller is `detectToolDenialInHistory` in the router: deferred-
 * execution residue in *history* counts as prompt pollution and triggers the
 * tool reality-check preamble on later turns.
 *
 * Pure function (no `this`, no I/O) for unit-testability.
 */

const INTENT_VERBS = [
  // 行动类
  '执行', '调用', '运行', '发起', '开始', '继续',
  // 读类
  '搜索', '查找', '查询', '检查', '获取', '读取', '查看', '列出',
  // 探查类
  '探查', '探测', '处理', '试一下', '试试',
  // 异步 follow-up 类（看到这些词意味着模型答应了"再调一次工具"）
  '轮询', '等待', '等结果', '等待结果', '查看结果', '看结果',
  '跟进', '跟踪', '监控', '确认结果',
] as const;

const VERB_ALT = INTENT_VERBS.join('|');

// 宽松主语（用于初筛 hasIntent）。允许 '然后' / '下一步' 这些过渡词；rule ③
// 用更严格的子集避免误伤过去时陈述。
const LOOSE_SUBJECTS = [
  '现在', '接下来', '下一步', '然后',
  // '我' 必须带未来时态修饰词。
  '我(将|会|要|马上|立刻|想|想要|准备|现在|即将)', '我来', '我去',
  '马上', '即将', '正要', '正在', '正',
  '准备', '即刻',
];
const LOOSE_SUBJECT_ALT = LOOSE_SUBJECTS.join('|');
const CN_INTENT_LOOSE = new RegExp(
  `(${LOOSE_SUBJECT_ALT})[^\\n。！？]{0,10}(${VERB_ALT})`,
);

// 严格主语集合：只接受真正暗示"未来 / 马上"语态的词。
//   - 不含 '然后' / '下一步'：这些既可能是计划也可能是回顾，rule ③ 会过紧。
//   - 不含 '正' / '继续'：歧义太大。
const STRICT_FUTURE_SUBJECTS = [
  '现在', '接下来', '马上', '即将', '正要', '正在', '准备', '即刻',
  '我(将|会|要|马上|立刻|想|想要|准备|现在|即将)', '我来', '我去',
];
const STRICT_SUBJECT_ALT = STRICT_FUTURE_SUBJECTS.join('|');
// rule ③ 专用：subject ─ {0~10 字非句号}（**不含**了/过/完，否则中间夹个
// 过去时标记就该放过）─ verb ─ **lookahead**：verb 后面不能立刻接 了/过/完/毕
const CN_INTENT_STRICT = new RegExp(
  `(${STRICT_SUBJECT_ALT})[^\\n。！？了过完]{0,10}(${VERB_ALT})(?![了过完毕])`,
);

// 英文：把动词的词干提出来，允许 -ing / -ed / -s 后缀；也允许 modal 和 verb
// 之间夹一个 adverb（"I'll now poll" / "I'll just go and poll" 这种）。
const EN_VERB_STEM =
  '(?:execute|call|run|invoke|search|query|fetch|read|list|poll|wait|follow\\s*up)';
const EN_VERB = `${EN_VERB_STEM}(?:ing|ed|s)?`;
const EN_ADVERB = '(?:now|just|then|first|go ahead and|now please)';
const EN_PATTERNS = [
  new RegExp(`now\\s+${EN_VERB}\\b`, 'i'),
  new RegExp(`let me\\s+(?:${EN_ADVERB}\\s+)?${EN_VERB}\\b`, 'i'),
  new RegExp(
    `(?:i'?ll|i will|i'?m going to|i'?m about to)\\s+(?:${EN_ADVERB}\\s+)?${EN_VERB}\\b`,
    'i',
  ),
  new RegExp(`(?:preparing|about)\\s+to\\s+${EN_VERB}\\b`, 'i'),
];

// ─── 澄清 / 条件式承诺收尾 ──────────────────────────────────────────────
// 真实误判 case（2026-07-05 日志）：模型收尾写
//   "告诉我你现在想做什么，我来帮你执行。"
//   "告诉我文件路径和需求，我立刻执行。"
// 这是在向用户征询输入、承诺"拿到信息后再执行"——合法的对话终点（等用户），
// 不是 deferred-execution。旧规则 ③ 命中"我来…执行"强制重试，模型换个说法
// 还是澄清式收尾，再次误判，两轮重试直接把 token 预算烧爆。
//
// 判定：征询/条件标记出现在意图动词**之前**（"告诉我 X，我就执行"），或整句
// 只有征询没有意图动词（"你可以告诉我："）。标记在动词之后不豁免——
// "我现在执行，如果失败会重试。"仍然算 deferred。
const CONDITIONAL_OFFER_MARKER = new RegExp(
  '(告诉我|告知我|请提供|请给出|请发|发给我|你可以|您可以|如果|若(你|您)|只要|一旦|' +
    '等(你|您)|需要(你|您)|你(想|要|希望|确认)|您(想|要|希望|确认)|' +
    'let me know|tell me|if you|once you|provide|send me|share)',
  'i',
);

function isConditionalOffer(sentence: string): boolean {
  if (!sentence) return false;
  const marker = CONDITIONAL_OFFER_MARKER.exec(sentence);
  if (!marker) return false;
  const verb = new RegExp(VERB_ALT).exec(sentence);
  if (!verb) return true;
  return marker.index < verb.index;
}

export function detectDeferredExecution(text: string): boolean {
  if (!text) return false;
  const trimmed = text.trim();
  // 6 字符门槛：避免极端短句（"好的"）触发，又能放过"现在轮询" 4 字 + 标点。
  if (trimmed.length < 6) return false;

  const hasIntent =
    CN_INTENT_LOOSE.test(trimmed) || EN_PATTERNS.some((p) => p.test(trimmed));

  // 收尾标点：省略号 / 半角省略号 / 全角省略号 / 冒号 — 都意味着话没说完
  const trailingEllipsis = /(\.{3,}|…|……)\s*$/.test(trimmed);
  const trailingColon = /[:：]\s*$/.test(trimmed);
  const lastSentence =
    trimmed.split(/[\n。！？!?]/).filter(Boolean).pop()?.trim() ?? '';
  const lastEllipsis = /(\.{3,}|…|……)\s*$/.test(lastSentence);
  const lastColon = /[:：]\s*$/.test(lastSentence);
  const lastIntentLoose =
    CN_INTENT_LOOSE.test(lastSentence) ||
    EN_PATTERNS.some((p) => p.test(lastSentence));
  const lastIntentStrict =
    CN_INTENT_STRICT.test(lastSentence) ||
    EN_PATTERNS.some((p) => p.test(lastSentence));
  const looksOpenEnded =
    trailingEllipsis || trailingColon || lastEllipsis || lastColon;

  // 澄清式收尾豁免：最后一句是"给我信息 → 我再执行"的条件承诺，
  // 模型在等用户输入，不是把执行"说"完就跑。
  const lastConditionalOffer = isConditionalOffer(lastSentence);

  // ① 意图词 + 任一开放式收尾：高置信，直接判 deferred
  if (hasIntent && looksOpenEnded && !lastConditionalOffer) return true;
  // ② 最后一句既有意图词又有省略号 / 冒号：高置信
  if (lastIntentLoose && (lastEllipsis || lastColon) && !lastConditionalOffer) return true;
  // ③ **最后一句**用**严格**未来主语 + 意图动词、且 verb 后不接过去时
  //    标记 (了/过/完/毕) → 即使尾巴是句号也算 deferred。
  //    历史 bug 复现："任务已排队……现在轮询结果。" 用句号收尾骗过旧规则。
  //    严格主语保证不误伤 "然后调用了 X" 这种回顾叙述。
  if (lastIntentStrict && !lastConditionalOffer) return true;
  // ④ 纯省略号收尾且整段很短（< 400 字）：保守兜底；防止"很长的最终结论 …
  //    末尾哲学省略号"被误判
  if ((trailingEllipsis || lastEllipsis) && trimmed.length < 400) return true;

  return false;
}
