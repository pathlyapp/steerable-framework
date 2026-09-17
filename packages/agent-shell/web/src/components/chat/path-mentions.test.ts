import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const resolveLocalPaths = vi.fn();
vi.mock('@/lib/local-api', () => ({
  resolveLocalPaths: (...args: unknown[]) => resolveLocalPaths(...args),
}));

const { looksLikeFilePath, peekResolvedPath, subscribeResolvedPath, resetPathMentionCache } =
  await import('./path-mentions');

beforeEach(() => {
  resetPathMentionCache();
  resolveLocalPaths.mockReset();
  resolveLocalPaths.mockResolvedValue({ resolved: [] });
});

afterEach(() => {
  resetPathMentionCache();
});

describe('looksLikeFilePath', () => {
  it('接受带分隔符、~/ 前缀、Windows 盘符与带扩展名的裸文件名', () => {
    for (const text of [
      './自我介绍.pptx',
      '/Users/me/报告.pdf',
      '../docs/a.md',
      'docs/report.md',
      '~/Downloads/x.zip',
      'C:\\Users\\me\\a.txt',
      '季度报告.pptx',
      'docs/',
    ]) {
      expect(looksLikeFilePath(text), text).toBe(true);
    }
  });

  it('拒绝命令、URL、版本号与空白串', () => {
    for (const text of [
      'npm install',
      'pnpm run dev:bs',
      'https://example.com/a.pdf',
      'http://x/y',
      '1.0.0',
      'v0.6.20',
      'someFunction',
      'CONSTANT_NAME',
      '',
      '   ',
    ]) {
      expect(looksLikeFilePath(text), text).toBe(false);
    }
  });

  it('拒绝超长候选（与后端上限一致）', () => {
    expect(looksLikeFilePath(`/tmp/${'a'.repeat(600)}.txt`)).toBe(false);
  });
});

describe('subscribeResolvedPath', () => {
  it('同一批订阅合并成一次请求，命中的回条目、未命中的回 null', async () => {
    resolveLocalPaths.mockResolvedValue({
      resolved: [{ candidate: './a.pptx', path: '/proj/a.pptx', isDirectory: false }],
    });
    const hits: unknown[] = [];
    subscribeResolvedPath('./a.pptx', 'chat-1', (entry) => hits.push(entry));
    subscribeResolvedPath('./missing.md', 'chat-1', (entry) => hits.push(entry));

    await vi.waitFor(() => expect(hits).toHaveLength(2));
    expect(resolveLocalPaths).toHaveBeenCalledTimes(1);
    expect(resolveLocalPaths).toHaveBeenCalledWith(['./a.pptx', './missing.md'], 'chat-1');
    expect(hits).toEqual([
      { candidate: './a.pptx', path: '/proj/a.pptx', isDirectory: false },
      null,
    ]);
  });

  it('已解析过的候选同步回调且不再发请求（流式重渲染不会打成风暴）', async () => {
    resolveLocalPaths.mockResolvedValue({
      resolved: [{ candidate: './a.pptx', path: '/proj/a.pptx', isDirectory: false }],
    });
    subscribeResolvedPath('./a.pptx', 'chat-1', () => {});
    await vi.waitFor(() => expect(peekResolvedPath('./a.pptx', 'chat-1')).not.toBeUndefined());

    const seen: unknown[] = [];
    subscribeResolvedPath('./a.pptx', 'chat-1', (entry) => seen.push(entry));
    expect(seen).toEqual([{ candidate: './a.pptx', path: '/proj/a.pptx', isDirectory: false }]);
    expect(resolveLocalPaths).toHaveBeenCalledTimes(1);
  });

  it('不同对话各自解析（相对路径的基准目录不同）', async () => {
    subscribeResolvedPath('./a.pptx', 'chat-1', () => {});
    subscribeResolvedPath('./a.pptx', 'chat-2', () => {});
    await vi.waitFor(() => expect(resolveLocalPaths).toHaveBeenCalledTimes(2));
    expect(resolveLocalPaths.mock.calls.map((c) => c[1])).toEqual(['chat-1', 'chat-2']);
  });

  it('后端不可达时落 null 缓存，不抛给渲染层', async () => {
    resolveLocalPaths.mockRejectedValue(new Error('backend down'));
    const seen: unknown[] = [];
    subscribeResolvedPath('./a.pptx', 'chat-1', (entry) => seen.push(entry));
    await vi.waitFor(() => expect(seen).toEqual([null]));
    expect(peekResolvedPath('./a.pptx', 'chat-1')).toBeNull();
  });

  it('取消订阅后不再回调', async () => {
    const onResolve = vi.fn();
    const unsubscribe = subscribeResolvedPath('./a.pptx', 'chat-1', onResolve);
    unsubscribe();
    await vi.waitFor(() => expect(resolveLocalPaths).toHaveBeenCalledTimes(1));
    expect(onResolve).not.toHaveBeenCalled();
  });
});
