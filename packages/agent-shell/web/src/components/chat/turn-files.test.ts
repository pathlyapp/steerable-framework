/**
 * turn-files 前端模型测试：parseTurnFiles 的形状校验（SSE 事件与持久化
 * 元数据共用一个解析器，坏数据一律 null 而不是半吊子列表），以及路径
 * 分段 / 文件大小的展示辅助。
 */
import { describe, expect, it } from 'vitest';

import { formatFileSize, parseTurnFiles, splitTurnFilePath } from './turn-files';

describe('parseTurnFiles', () => {
  it('合法列表原样解析（size 可选）', () => {
    const files = parseTurnFiles([
      { path: '/proj/自我介绍.pptx', kind: 'created', size: 1024 },
      { path: '/proj/README.md', kind: 'modified' },
    ]);
    expect(files).toEqual([
      { path: '/proj/自我介绍.pptx', kind: 'created', size: 1024 },
      { path: '/proj/README.md', kind: 'modified' },
    ]);
  });

  it('空数组 / 非数组 → null（调用方按「无列表」处理）', () => {
    expect(parseTurnFiles([])).toBeNull();
    expect(parseTurnFiles('files')).toBeNull();
    expect(parseTurnFiles(undefined)).toBeNull();
  });

  it('任一条目形状不符 → 整体 null', () => {
    expect(parseTurnFiles([{ path: '/a', kind: 'created' }, { path: 1, kind: 'created' }])).toBeNull();
    expect(parseTurnFiles([{ path: '/a' }])).toBeNull();
    expect(parseTurnFiles([{ path: '/a', kind: 'deleted' }])).toBeNull();
    expect(parseTurnFiles([null])).toBeNull();
  });
});

describe('splitTurnFilePath', () => {
  it('POSIX 路径拆成目录 + 文件名', () => {
    expect(splitTurnFilePath('/proj/out/result.txt')).toEqual({
      dir: '/proj/out/',
      name: 'result.txt',
    });
  });

  it('Windows 路径同样可拆', () => {
    expect(splitTurnFilePath('C:\\work\\proj\\a.md')).toEqual({
      dir: 'C:\\work\\proj\\',
      name: 'a.md',
    });
  });

  it('裸文件名没有目录部分；尾部斜杠先归一', () => {
    expect(splitTurnFilePath('a.md')).toEqual({ dir: '', name: 'a.md' });
    expect(splitTurnFilePath('/proj/out/')).toEqual({ dir: '/proj/', name: 'out' });
  });
});

describe('formatFileSize', () => {
  it('按量级取 B / KB / MB；缺省与非法值返回 null', () => {
    expect(formatFileSize(512)).toBe('512 B');
    expect(formatFileSize(2048)).toBe('2.0 KB');
    expect(formatFileSize(5 * 1024 * 1024)).toBe('5.0 MB');
    expect(formatFileSize(undefined)).toBeNull();
    expect(formatFileSize(-1)).toBeNull();
    expect(formatFileSize(Number.NaN)).toBeNull();
  });
});
