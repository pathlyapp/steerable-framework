import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { parse } from "yaml";
import { nextRetryDelayMs } from "@steerable/agent-harness";
describe("conformance retry", () => {
    it("matches retry case", () => {
        const file = path.resolve(process.cwd(), "../cases/retry/basic.yaml");
        const data = parse(fs.readFileSync(file, "utf8"));
        const actual = data.attempts.map((attempt) => nextRetryDelayMs({
            maxAttempts: data.policy.maxAttempts,
            baseDelayMs: data.policy.baseDelayMs,
            maxDelayMs: data.policy.maxDelayMs,
            jitter: data.policy.jitter,
        }, attempt));
        expect(actual).toEqual(data.expected);
    });
});
