# CoreLoop 下沉 · agent 先行改造清单

> 目标：把 deeppath-agent 的单 agent 循环下沉为 steerable-agent-runtime 的
> CoreLoop（Python），由 sidecar 托管，Electron 通过反向通道回调执行工具，
> 最终删掉 deeppath-agent/src/harness。deeppath-api 采纳排到最后，可选。
>
> 决策背景：见 2026-08-25 的边界评审与迁移计划（两份 canvas）。
> 关键前提：CoreLoop 用 Python 写进 agent-runtime，不是 TypeScript；
> LoopEvent 接口按 deeppath-api 的需求设计，实现按 deeppath-agent 验证。

---

## 两个必须避开的陷阱

- [ ] **不用 TypeScript 写 CoreLoop** —— 框架 Tier2/Tier3 是 Python-only，
      TS 侧只是一致性测试面。写成 TS 就是第四份实现，api 永远接不进来。
- [ ] **LoopEvent 分类不按 agent 的 8 种 SSE 形状设计** —— 按 api 约 100 个
      发射点归纳出的分类设计，否则 api 到时候接不进来。

## 硬阻塞（A3 之前必须解决）

- [ ] **sidecar 反向通道**：当前 stdio JSON-RPC 是请求半双工，sidecar 不能
      向 host 发起带响应的请求。而「Python 跑 loop、工具在 Electron 执行」
      恰恰需要这个能力。见 A1。
- [ ] **包体**：sidecar 实测 700–740MB（CI 预算 780MB），320MB 目标未达成。
      开发/canary 可用系统 python 绕过，正式发版前必须解决。

---

## A0 · 盘点 api，产出港口规格 ✅ 已完成（2026-08-25）

不改 deeppath-api 一行代码，只读。已交付：

- [x] **港口规格**：`docs/spec/core-loop.md` —— LoopEvent 五类分类
      （生命周期 / 内容流 / 工具侧 / 证据 / 预算控制）、ToolExecutor 端口
      （10 个 if/elif 分支 → 注册式处理器映射表）、产品侧 hook 清单、
      待统一的语义分歧。已上线 https://steerableframework.com/spec/core-loop/
- [x] **一致性用例**：`tests/conformance/cases/{policy,budget,completion}/`
      + 两侧 runner（`test_{policy,budget,completion}_conformance.py` /
      `{policy,budget,completion}.test.ts`）。Py 4 + TS 35 全绿，
      已进 CI（commit `30343ac`，CI run success）。
- [x] **agent 死代码清理**：删 `extractIdempotencyKey` / `stripHarnessMetadata`
      （生产零引用），agent `9888ab3`，harness 测试 35 通过。

**勘察发现（影响后续阶段）**：
- api 的 loop 直接 `yield` SSE 字节，约 100 个发射点，无内部事件抽象——
  A5 的最大工作量在这。
- `_run_tool_calls`（loop.py 4805–5554）是 10 分支 if/elif，横切关注点
  （去重/策略/预算）与产品执行混在一起。
- orchestrator.py 已把 HarnessLoop 当黑盒 worker 用——编排与单步的缝已存在。
- **agent 仓库没有跑测试的 CI**（只有 build-windows + pages）。A1 会同时动
  framework + agent，建议先给 agent 补一个跑 vitest 的 workflow。
- api 的 `trajectory_eval`（离线 JSONL 评分）与 `replay`（live stageData
  压缩轨迹）是两套系统；录制已在生产路径，但「导出→评分→回归」未产品化。

## A1 · sidecar 反向通道（1 周，阻塞项）

涉及 steerable-framework + deeppath-agent，与 api 无关。

- [ ] **前置**：给 deeppath-agent 补一个跑 `vitest` 的 CI workflow
      （当前只有 build-windows + pages，A0 删代码只能靠本地验证，无线上兜底）
- [ ] 扩展 `spec/sidecar/*.schema.json`：sidecar→host 的请求/响应帧
- [ ] 改 `packages/agent-runtime/py/.../transport/stdio_jsonrpc.py`：
      sidecar 侧能发起带 id 的请求并等待响应
- [ ] 改 `deeppath-agent/src/sidecar/supervisor.ts`：能服务来自 sidecar 的请求
      （当前收到带 id 的帧只当响应处理）
- [ ] 在 ToolRouter 上加远程代理工具的注册助手（当前只有进程内可调用工具，
      见 `packages/agent-runtime/py/.../tools.py` 的 dispatch）
- [ ] 端到端 example：sidecar 发起请求 → Electron 执行 shell → 结果回到 sidecar

**通过标准**：上面的端到端 example 跑通。
**回滚**：新增协议方法，旧路径不动，不启用即可。

## A2 · agent 轨迹录制与回放（3–5 天，可与 A1 并行）

api 有 trajectory_eval.py + replay.py，agent 没有。这层安全网必须先建，
否则 A3 的 Python 重写没法证明行为没变。

- [ ] 基于现有 `harness_traces` 表 + `saveTrace` + 消息上的 `executedActions`
      补录制
- [ ] 补逐事件回放（对齐 api 的 `reduce_execution_state` 思路）
- [ ] 先存档 ≥20 条覆盖主要场景的真实桌面轨迹

**通过标准**：能录制并逐事件回放至少 20 条真实轨迹。
**备注**：这套录制回放能力本身以后该进框架。

## A3 · CoreLoop v0（Python，4–6 周，最大工作量）

在 `packages/agent-runtime/py` 里实现。这是跨语言重写，不是原地重构。

下沉的（通用机制）：
- [ ] 内外双层循环状态机与轮次控制
- [ ] LLM 流消费、UTF-16 代理对修复、推理内容提取
- [ ] 伪函数调用 / markdown 工具调用的识别与恢复
- [ ] 预算计数、软超时、压缩续跑、轮次扩展
- [ ] 大结果外置为 artifact
- [ ] 工具去重、未知工具建议、参数 schema 强制转换
- [ ] **agent 独有的反幻觉层**（这是 api 缺的，是净增益）：
      data-need 路由、grounding 判定、deferred/claimed 重试、narration round

留在产品侧的（不下沉）：dp-action 提案、UI 工具、response 标签契约、
context_system 分层、目标校验器、技能预算、计费、时区、实体查库、桌面中继、
编排/群聊/协作。

**推进方式**：按切片，反幻觉层放最后一片；之前 sidecar 路径只在 canary 开启。
**通过标准**：回放 A2 的轨迹，Python CoreLoop 的决策序列与 TS 版逐事件一致
（差异需逐条可解释）。
**回滚**：CoreLoop 只在 flag 下启用，默认仍走 TS 路径。

## A4 · desktop 切换并删码（2–3 周）

- [ ] sidecar 托管 CoreLoop，工具走 A1 的反向通道回 Electron 执行
- [ ] 灰度通过后删 `deeppath-agent/src/harness`
- [ ] 把 61 条 shell 安全规则回流到框架（框架现在只有 6 条存根）
- [ ] 解决包体门禁（见上方硬阻塞）

**通过标准**：sidecar 模式下离线 Ollama + 本地 shell/文件/MCP 工具全部跑通；
包体过门禁。
**回滚**：关掉 STEERABLE_USE_SIDECAR 即回落本地 TS 路径。

## A5 · api 采纳（可选，以后）

- [ ] api 侧最大的活：把约 100 个 SSE 发射点改成结构化事件
- [ ] 此时 CoreLoop 已被真实产品验证，且带着 api 缺失的反幻觉能力

---

## 中途停靠点

只做 A0 + A1 + A2（约三周）也成立。拿到：反向通道、回放靶场、
基于 api 真实需求的港口规格。三样东西无论后面怎么选都不浪费，
到那再决定是否投入 A3 的重写。

## 已发生的漂移 · 处置决定（2026-08-25）

- [x] **跨语言一致性用例** —— 已补 policy / budget / completion（见 A0）。
- [~] **`maxToolErrors`：推迟到 CoreLoop 统一，A0 不改代码。**
      勘察发现两端语义根本不同，不能对齐成一个数：
      api 是**累计**工具错误（完成判定硬编码 2，预算断路器默认 3，
      api 内部自己都不一致）；agent 是**连续**失败、成功即清零（阈值 3，
      故意如此——累计制会惩罚「失败后换路径最终成功」的任务）。
      CoreLoop 需选定一种语义（建议连续）并做成可配置。
      已记录进 `docs/spec/core-loop.md`。
- [~] **token 预算：保持不同，只记录。** api 120k（服务端大上下文）、
      agent 60k（本地小上下文）是刻意的，做成可配置即可，不强求一个数。
      已记录进规格。
- [ ] **死桥接函数**：`consume_framework_budget` 等在 api 侧（定义了没人调）。
      因「不动 api」，本次未处理，留待 A5。
      agent 侧的死导出已在 A0 清掉。
