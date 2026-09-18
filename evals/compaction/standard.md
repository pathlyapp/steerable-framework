# 压缩策略评测标准（compaction-bench）

评价一个压缩策略好不好，不看它声明了什么机制，看它在受控压力下的
可测量行为。本标准分两层：**模拟器层**（确定性、零成本、CI 可跑）
和 **Harbor 实盘层**（真实模型、真实任务、按量付费）。模拟器层是
门槛——一个策略连确定性探针都过不了，不值得花实盘的钱。

## 独立性条款（为什么这个标准是客观的）

1. **任务生成与策略无关。** 任务内容（blob、针、提问）由固定种子
   生成，任何臂的策略参数不参与生成过程。
2. **判分是精确匹配，不用裁判模型。** 针的格式
   `NDL-<idx>:<secret>`，secret 由种子决定；manifest 里的针与
   种植集合逐一对号。对错没有解释空间。
3. **策略对"模型"不可见。** 模拟器里的 policy provider 只读传入
   请求的消息内容，永远看不到钩子配置——臂间差异只能来自压缩策略
   对 transcript 的改写。
4. **指标从录制请求计算，不采信框架自报。** prompt tokens 是策略
   实际发出的请求估算之和；缓存命中率是相邻请求的字节前缀复用率。
5. **标准必须先过区分度自检。** 已知差策略（无压缩、朴素截断）必须
   在本标准下显著差于当前实现；若区分不开，说明标准失灵，先修标准。
   自检由 `evals/tests/test_compaction_sim.py` 钉死在 CI。

## 任务族

| 任务 | 轮数 | blob/轮 | 针/轮 | 叙述/轮 | 窗口 | 总针数 |
|---|---|---|---|---|---|---|
| `light` | 12 | 1.5KB | 2 | 6KB | 8k | 24 |
| `heavy` | 30 | 1.5KB | 3 | 6KB | 16k | 90 |

设计要点：assistant 叙述（不可 fold）主导压力，所以每次压缩都会
走到 summarize 阶段——套件量的是**摘要保真度**，不只是 fold 摘录
的运气。针在 blob 内等距分布，fold 摘录窗口只能碰巧保住头尾几根。

## 指标

| 指标 | 定义 | 好 = |
|---|---|---|
| `success` | 全程无致命错误且针召回率 = 1.0 | ✅ |
| `needle_recall` | manifest 中正确针数 / 种植针数 | 1.0 |
| `compactions` | 压缩触发次数 | 少而有效 |
| `overflows` | 真实 context_overflow 次数 | 0（恢复不算错误） |
| `prompt_tokens_total` | 所有成功请求的估算 token 和 | 小 |
| `cache_hit_ratio` | 相邻请求字节前缀复用率（按请求大小加权） | 接近 1 |

## 臂（arms）

| 臂 | 含义 | 预期表现 |
|---|---|---|
| `none` | 无压缩（NoopHooks） | 窗口打满即死，recall 0 |
| `naive` | 朴素截断中段（无摘要无摘录） | 只有 tail 里的针幸存，recall 低 |
| `fold_only` | 现实现去掉 summarizer（确定性摘录兜底） | 摘录窗口外的针丢失，recall 中低 |
| `current` | 当前实现（fold + warm-prefix 回放摘要） | recall 1.0 |

## 运行

```bash
# 模拟器层（无需 API key）
python -m evals.compaction.run_sim                     # 全臂全任务
python -m evals.compaction.run_sim --arms current --tasks heavy

# 区分度自检
.venv/bin/pytest evals/tests/test_compaction_sim.py
```

## Harbor 实盘层（需要 STEERABLE_API_KEY，跨框架）

模拟器层证明机制差异；实盘层证明生态效度——真实模型的摘要不是
完美抽取式的，真实任务的压力形状也不规则。实盘复用既有
`evals/harbor_*` 五家 runner，任务集是为压缩设计的 Harbor 任务
（确定性大输出 + 精确判分），同一模型（DeepSeek）跑五家，指标同上，
另加缓存命中 token 占比（从 provider usage 读出）。

实盘层的任务定义与运行协议见 `evals/compaction/harbor/`（待落地，
需要 API key 时先问用户）。

## 用标准驱动升级的纪律

1. 先在本标准下跑**老架构基线**，数据入档。
2. 架构升级（P1 区段事务 / P2 图像 offload / P3 per-model 策略）
   每一步必须让至少一个指标变好、且没有指标显著变差，否则不合入。
3. 升级后复测的全部数据与基线同表对比，写进 `docs/comparison.md`
   的压缩轴证据。
