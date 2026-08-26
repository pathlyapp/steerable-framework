import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { parse } from "yaml";
import { decideToolMode } from "@steerable/agent-harness";
describe("conformance policy", () => {
    it("matches decide_tool_mode case", () => {
        const file = path.resolve(process.cwd(), "../cases/policy/decide_tool_mode.yaml");
        const data = parse(fs.readFileSync(file, "utf8"));
        const actual = data.inputs.map((name) => decideToolMode(name));
        expect(actual).toEqual(data.expected);
    });
});
