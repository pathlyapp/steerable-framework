import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { parse } from "yaml";
import { consumeBudget } from "@steerable/agent-harness";
describe("conformance budget", () => {
    it("matches consume_budget case", () => {
        const file = path.resolve(process.cwd(), "../cases/budget/consume.yaml");
        const data = parse(fs.readFileSync(file, "utf8"));
        const limits = {
            maxTokens: data.limits.maxTokens,
            maxSteps: data.limits.maxSteps,
            maxToolCalls: data.limits.maxToolCalls,
        };
        let state = { tokensUsed: 0, stepsUsed: 0, toolCallsUsed: 0 };
        const actual = data.ops.map((op) => {
            const result = consumeBudget(state, limits, {
                tokens: op.tokens ?? 0,
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
    });
});
