import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { parse } from "yaml";
import { consumeBudget, type BudgetState } from "@steerable/agent-harness";

interface BudgetOp {
  tokens?: number;
  cachedTokens?: number;
  step?: boolean;
  toolCall?: boolean;
}

function runCase(name: string): void {
  const file = path.resolve(process.cwd(), `../cases/budget/${name}`);
  const data = parse(fs.readFileSync(file, "utf8"));
  const limits = {
    maxTokens: data.limits.maxTokens,
    maxSteps: data.limits.maxSteps,
    maxToolCalls: data.limits.maxToolCalls,
    ...(data.limits.cachedTokenWeight !== undefined
      ? { cachedTokenWeight: data.limits.cachedTokenWeight }
      : {}),
  };
  let state: BudgetState = { tokensUsed: 0, stepsUsed: 0, toolCallsUsed: 0 };
  const actual = data.ops.map((op: BudgetOp) => {
    const result = consumeBudget(state, limits, {
      tokens: op.tokens ?? 0,
      cachedTokens: op.cachedTokens ?? 0,
      step: op.step ?? false,
      toolCall: op.toolCall ?? false,
    });
    state = result.state;
    return {
      tokensUsed: state.tokensUsed,
      stepsUsed: state.stepsUsed,
      toolCallsUsed: state.toolCallsUsed,
      exhausted: result.exhausted,
    };
  });
  expect(actual).toEqual(data.expected);
}

describe("conformance budget", () => {
  it("matches consume_budget case", () => {
    runCase("consume.yaml");
  });

  it("matches consume_budget cached-discount case", () => {
    runCase("cached.yaml");
  });
});
