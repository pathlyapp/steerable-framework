import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { parse } from "yaml";
import { isTerminalResult } from "@steerable/agent-harness";

describe("conformance completion", () => {
  it("matches is_terminal_result case", () => {
    const file = path.resolve(
      process.cwd(),
      "../cases/completion/is_terminal_result.yaml"
    );
    const data = parse(fs.readFileSync(file, "utf8"));
    const actual = data.inputs.map(
      (result: Record<string, unknown> | null) => isTerminalResult(result)
    );
    expect(actual).toEqual(data.expected);
  });
});
