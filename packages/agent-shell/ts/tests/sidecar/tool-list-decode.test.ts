import { describe, expect, it } from 'vitest';
import { toolNamesFromDescriptors } from '../../src/sidecar/supervisor.js';

// `tool.list` 的可用集是 web 工具握手的真相源（main.ts）。sidecar 返回的是
// OpenAI function-call 描述符，名字在 `function.name`；按顶层 `name` 读会
// 得到一串 undefined，表现为"sidecar 什么都没注册"而不是解码失败——桌面
// 因此一度从不暴露 web 工具。这里的输入是真实 `tool.list` 回包的原样形状。
const REAL_TOOL_LIST = [
  {
    type: 'function',
    function: {
      name: 'web_fetch',
      description:
        'Fetch one public web page over http(s) and return its text (HTML is converted to plain text). Private/loopback/link-local targets are refused; cross-origin redirects are reported, not followed — re-issue the call with the reported URL.',
      parameters: {
        type: 'object',
        properties: { url: { type: 'string', description: 'The http(s) URL to fetch.' } },
        required: ['url'],
        additionalProperties: false,
      },
    },
  },
];

describe('tool.list 描述符解码', () => {
  it('从真实回包形状取出工具名', () => {
    expect(toolNamesFromDescriptors(REAL_TOOL_LIST)).toEqual(['web_fetch']);
  });

  it('顶层 name 不是名字所在处：只有 function.name 才算', () => {
    expect(toolNamesFromDescriptors([{ type: 'function', name: 'web_fetch' }])).toEqual([]);
  });

  it('空注册表与非数组回包都得到空集，不抛', () => {
    expect(toolNamesFromDescriptors([])).toEqual([]);
    expect(toolNamesFromDescriptors(null)).toEqual([]);
    expect(toolNamesFromDescriptors({ tools: REAL_TOOL_LIST })).toEqual([]);
  });

  it('跳过残缺条目而不是整包放弃', () => {
    expect(
      toolNamesFromDescriptors([
        null,
        { type: 'function' },
        { type: 'function', function: {} },
        { type: 'function', function: { name: '' } },
        ...REAL_TOOL_LIST,
      ]),
    ).toEqual(['web_fetch']);
  });
});
