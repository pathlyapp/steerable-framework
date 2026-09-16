---
name: local-exec
description: Local shell / filesystem control via local_exec_shell / local_read_file / local_write_file. Loaded only when the host actually exposes these tools.
priority: 700
tags: [local, shell, filesystem]
conditions: [tool:local_exec_shell, tool:local_read_file, tool:local_write_file]
match: any
---

# 本地终端 / 文件控制

当用户请求执行任何 shell / 终端命令（"pwd"、"ls"、"运行 xxx"、"执行 xxx"、"看下目录"、"看下文件内容"…）时：

| 用户意图 | 必须使用的工具 |
| --- | --- |
| 执行命令 / 跑脚本 | `local_exec_shell`（参数 `command` 为完整命令） |
| 写代码片段解决问题（Python/Node，可自动装依赖） | `local_run_snippet`（`language` + `code`，可选 `pipPackages` / `npmPackages`） |
| 查看文件 | `local_read_file`（`path` 为绝对路径） |
| 写入 / 修改文件 | `local_write_file`（`path` + `content`） |
| 打开文件 / URL | `local_open_path` |
| 调用已注册脚本 | `local_list_scripts` → `local_run_script` |

## 规则

- **不允许只用文字回答** "你应该执行 …"——你应当**直接发 tool_call** 让命令真正跑起来，然后基于真实输出汇报。
- 命令可能改文件、装包、删数据时（destructive 类操作），**先**简单告诉用户你要执行什么，**再**发 tool_call；不要中间停手等用户确认（用户已经请求了）。
- `local_exec_shell` 返回包含 `stdout` / `stderr` / `exitCode`。非 0 退出码视为失败：把 stderr 摘要给用户，并建议下一步。
- 路径不存在 / 权限不足时，把 fs 错误原文告诉用户，**不要**反复重试同一个路径。


- **超时 ≠ 失败，严禁原样重跑启动命令**：返回里出现 `timedOut: true` 时，只是 shell 等待被掐断——如果命令是启动 GUI / 长时间运行的程序（如 Qt 界面软件），该程序**很可能仍在运行**，用户可能正在里面操作。此时**不要**再次执行同一条启动命令（会把程序重复启动多次）。正确做法：先确认进程是否在运行（如 `Get-Process`）；确实需要长时间等待时传更大的 `timeout` 参数；启动 GUI 程序时优先用不阻塞的方式（PowerShell `Start-Process "app.exe"`），启动成功即视为完成，不要等它退出。
- **GUI 启动命令的特殊约定**：命令行里带独立的 `gui` 字样（如 `xxx.exe gui replay ...`、`--gui`）会被识别为"启动 GUI 程序"。这类命令等待一小段时间后若程序仍在运行，会返回 `success: true` + `stillRunning: true`（程序不会被终止）——这**就是成功**，表示界面已拉起、等用户操作。看到 `stillRunning: true` 后直接向用户汇报"程序已启动，请在界面中操作"，**绝对不要**再次执行同一条命令。

## 真实执行纪律

- **没有真实 tool_call，就等于没有执行。** 不允许写"我已经在终端运行了"、"terminal 输出如下"、"命令执行完成"来代替 `local_exec_shell`。
- 只有在看到 `local_exec_shell` 的真实返回后，才能说"已运行"、"执行成功/失败"、"输出为..."。
- 汇报命令结果时必须基于返回字段：
  - `success: true` / `exitCode: 0` → 可以说执行成功，并摘要 `stdout`。
  - `success: false` / 非 0 `exitCode` → 必须说执行失败，并摘要 `stderr` 或 `error`。
  - `stdout` / `stderr` 为空 → 不要编造输出；直接说命令没有输出或错误流为空。
- 如果你只是想说明下一步，请立刻发起对应 tool_call；不要停在"现在执行..."、"接下来运行..."、"我来查一下..."。

## Windows 命令规范（按真实 shell 选方言）

当前用户环境通常是 Windows。`local_exec_shell` 在 Windows 上默认使用 **PowerShell**，但用户也可能切到 cmd。**不要凭空假设方言**：

- `local_exec_shell` 返回里带 `shell` 字段（`powershell` / `cmd` / `wsl` / `bash` / `zsh`）。**第一条命令跑完后，看 `shell` 字段确认实际方言，后续命令按它来写。**
- 不确定时，第一条命令优先用两种 shell 都成立的探测命令，例如 `cd`（cmd/PowerShell 都能打印或切换）或直接读返回的 `shell` 字段。
- 路径含空格必须加双引号，例如 `"C:\Users\name\My Documents"`。

### PowerShell（Windows 默认）常用命令

| 目的 | 命令 |
| --- | --- |
| 查看当前位置 | `Get-Location` |
| 列出当前目录 | `Get-ChildItem`（含隐藏项加 `-Force`） |
| 进入目录 | 优先用 `local_exec_shell.cwd` 参数，而非 `Set-Location` |
| 读取文本文件 | `Get-Content "C:\path\file.txt" -Raw`（前 N 行用 `-TotalCount 50`） |
| 搜索文件内容 | 先 `Get-Command rg` 探测：可用则 `rg "词" "C:\dir"`，否则 `Select-String -Path "C:\dir\*" -Pattern "词"` |
| 查找文件名 | `Get-ChildItem "C:\dir" -Recurse -Filter "*.ts"` |
| 创建目录 | `New-Item -ItemType Directory -Force "C:\new-dir"` |
| 复制 / 移动 / 删除 | `Copy-Item` / `Move-Item` / `Remove-Item`（删除谨慎） |
| 查看环境变量 | `$env:PATH` |
| 设置本次命令环境变量 | `$env:NODE_ENV="development"; pnpm dev` |
| 运行 exe / 带空格路径 | `& "C:\Program Files\App\tool.exe" --help` |
| 查看命令是否存在 | `Get-Command pnpm` |
| 过滤进程 | `Get-Process | Where-Object { $_.ProcessName -like "*node*" }` |

### cmd.exe 等价命令（当 `shell` 字段为 `cmd` 时）

| 目的 | 命令 |
| --- | --- |
| 列出目录 | `dir` |
| 读取文本文件 | `type "C:\path\file.txt"` |
| 搜索文件内容 | `findstr /s "词" "C:\dir\*"` |
| 查看环境变量 | `echo %PATH%` |
| 复制 / 移动 / 删除 | `copy` / `move` / `del` |

### 注意事项

- **`shell` 为 `cmd` 时不要发 PowerShell cmdlet**（`Get-ChildItem`、`$env:`、`& "..."` 在 cmd 里会报错）；反之 cmd 命令在 PowerShell 多数也能用，但 `%VAR%` 变量语法不通用。
- 不要默认套用 Unix 专属写法（`cat`、`grep`、`find`、`rm`、`cp`、`mv`）；它们在 cmd 下不存在，在 PowerShell 下虽有别名但应优先用上面的原生写法。
- PowerShell 里 `;` 只表示顺序执行，**不**代表前一个成功才执行后一个；需按成功继续用 `命令A; if ($LASTEXITCODE -eq 0) { 命令B }`。Windows PowerShell 5.1 不支持 Bash 风格 `&&` / `||`，除非真实返回证明支持，否则别依赖。
- 需要在特定目录运行时，优先设置 `local_exec_shell.cwd`，不要把很长的 `Set-Location ...; command` / `cd /d ... &&` 拼进命令里。
- 运行当前目录脚本用 `.\script.ps1` 或 `.\tool.exe`；若脚本执行策略报错，把真实错误告诉用户，不要假装运行成功。
