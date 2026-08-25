import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { parse } from "yaml";
import { classifyShellCommand } from "@steerable/agent-harness";

describe("conformance safety", () => {
  it("matches classify_shell_command case", () => {
    const file = path.resolve(
      process.cwd(),
      "../cases/safety/classify_shell_command.yaml",
    );
    const data = parse(fs.readFileSync(file, "utf8"));
    const actual = data.inputs.map((cmd: string) => classifyShellCommand(cmd));
    expect(actual).toEqual(data.expected);
  });
});
