/**
 * Cross-dialect command adaptation for Windows PowerShell 5.1.
 *
 * LLM 生成的命令经常混用 cmd / bash 语法（`&&`、`||`、`%VAR%`），而我们在
 * Windows 上默认用 `powershell.exe`（= Windows PowerShell 5.1，**不支持**
 * Bash 风格的 `&&` / `||` 链接符，也不认识 `%VAR%` 环境变量语法）。
 * 与其指望模型每次都写对方言，不如在执行前做一次确定性转换：
 *
 *   - `%VAR%`        → `$env:VAR`
 *   - `A && B`       → `A; if ($?) { B }`
 *   - `A || B`       → `A; if (-not $?) { B }`
 *   - `A && B && C`  → `A; if ($?) { B; if ($?) { C } }`（从右往左嵌套）
 *
 * 引号内的 `&&` / `||` / `%VAR%` 不会被改写（按单双引号做词法扫描）。
 * 转换是幂等的：已经是纯 PowerShell 语法的命令原样返回。
 */

/** 把 cmd 风格的 `%VAR%` 环境变量替换成 PowerShell 的 `$env:VAR`。 */
function replaceCmdEnvVars(command: string): string {
  let out = '';
  let quote: '"' | "'" | null = null;
  for (let i = 0; i < command.length; i++) {
    const ch = command[i];
    if (quote) {
      out += ch;
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      out += ch;
      continue;
    }
    if (ch === '%') {
      const m = command.slice(i).match(/^%([A-Za-z_][A-Za-z0-9_]*)%/);
      if (m) {
        out += `$env:${m[1]}`;
        i += m[0].length - 1;
        continue;
      }
    }
    out += ch;
  }
  return out;
}

/** 按引号感知拆分顶层 `&&` / `||` 链。没有链接符时返回 null。 */
function splitChain(
  command: string
): { parts: string[]; ops: Array<'&&' | '||'> } | null {
  const parts: string[] = [];
  const ops: Array<'&&' | '||'> = [];
  let current = '';
  let quote: '"' | "'" | null = null;
  for (let i = 0; i < command.length; i++) {
    const ch = command[i];
    if (quote) {
      current += ch;
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      current += ch;
      continue;
    }
    const two = command.slice(i, i + 2);
    if (two === '&&' || two === '||') {
      parts.push(current);
      ops.push(two);
      current = '';
      i++;
      continue;
    }
    current += ch;
  }
  parts.push(current);
  if (ops.length === 0) return null;
  const trimmed = parts.map(p => p.trim());
  // 任一段为空（如 "foo &&" 结尾）说明命令本身畸形，不做改写。
  if (trimmed.some(p => p.length === 0)) return null;
  return { parts: trimmed, ops };
}

/**
 * 把混用 cmd / bash 语法的命令转换成 Windows PowerShell 5.1 可执行的形式。
 * 对已经合法的 PowerShell 命令是 no-op。
 */
export function adaptCommandForPowerShell(command: string): string {
  if (!command) return command;
  let out = replaceCmdEnvVars(command);
  const chain = splitChain(out);
  if (chain) {
    const { parts, ops } = chain;
    // 从右往左嵌套构造，保持 && / || 的短路语义。
    let acc = parts[parts.length - 1];
    for (let i = ops.length - 1; i >= 0; i--) {
      const cond = ops[i] === '&&' ? '$?' : '-not $?';
      acc = `${parts[i]}; if (${cond}) { ${acc} }`;
    }
    out = acc;
  }
  return out;
}
