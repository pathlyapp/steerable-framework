---
name: proactive-coding
description: 主动编程补位——专用工具/技能搞不定或太绕时，用 local_run_snippet 编写并运行 Python/Node 脚本直接解决，可自动安装 pip/npm 依赖。仅当宿主暴露 local_run_snippet 时加载。
priority: 705
tags: [local, coding, python, node, automation]
conditions: [tool:local_run_snippet]
match: any
---

# 主动编程（写脚本解决问题）

你具备**本地编程能力**：当现有专用工具（场景包工具、MCP 工具、文件读写等）不足以完成任务，或者用它们要绕很多步时，**主动写一段脚本直接解决**，而不是回答"我做不到"或甩给用户一份手动步骤。

## 何时应该主动编程

- 数据处理 / 格式转换：CSV ↔ Excel ↔ JSON、日志解析、批量重命名、图片/PDF 处理
- 计算与校验：统计、对账、哈希、大数运算、日期推算
- 文本加工：正则提取、非结构化文本解析、批量替换
- 自动化胶水：组合多个 CLI 工具、轮询等待、批量调用
- 验证与排查：快速验证一个猜想、复现一个边界情况

一句话：**"写个脚本跑一下"比"一步步教用户做"更好时，就写脚本。**

## 工具选择

| 场景 | 工具 |
| --- | --- |
| 写代码片段并运行（首选） | `local_run_snippet`（`language` + `code`，一次调用 = 落盘 + 执行） |
| 交互式 / 长运行 / 启动程序 | `local_exec_shell` |
| 交付脚本给用户长期使用 | 先用 `local_run_snippet` 验证跑通，再用 `local_write_file` 写到用户指定位置并告知路径 |

`local_run_snippet` 参数：

- `language`: `"python"`（数据处理首选）或 `"node"`
- `code`: 完整可运行的源码（别留 `...` 占位）
- `pipPackages` / `npmPackages`: 运行前自动安装的依赖（见下）
- `cwd`: 工作目录；项目模式对话默认在项目根目录，脚本里用**相对路径**操作项目文件
- 返回含 `scriptPath`（片段保留在 scratch 目录，可复查）、`interpreter`、`installedPackages`

## 依赖安装

- **优先用标准库**；确需第三方包时，在 `pipPackages` / `npmPackages` 里声明，工具会自动安装（pip 用 `--user`，遇 PEP 668 限制自动改用 venv；npm 装进 scratch 目录，脚本可直接 `require`/`import`）。
- 装依赖前用一句话告诉用户要装什么、为什么（"需要安装 pandas 来读 Excel"），然后直接发 tool_call，不要停下来等确认。
- 安装失败：把真实错误告诉用户并给替代方案（换包 / 换语言 / 手动安装命令），不要假装装好了。

## 纪律

- **没跑过的代码不要说"运行结果如下"。** 只有看到 `local_run_snippet` 的真实返回（`exitCode: 0` + `stdout`）才能汇报结果；非 0 退出码就读 `stderr` 改代码重跑（最多迭代 2-3 次，还不行就如实说明卡点）。
- 脚本里读写用户文件时，路径用用户提供的真实路径或项目内相对路径；不要编造路径。
- 脚本产生的**交付物**（报表、转换后的文件）要落到用户能找到的地方（项目目录或用户指定路径），并在回复里给出完整路径——scratch 目录是临时区，不是交付位置。
- 代码保持简单直接：能 20 行解决就不要写 100 行；不要引入与任务无关的"工程化"。
