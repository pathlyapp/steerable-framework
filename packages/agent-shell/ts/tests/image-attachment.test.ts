import { describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import {
  computeTargetSize,
  isImagePath,
  parseImageAttachments,
  processImageAttachments,
  IMAGE_MAX_SOURCE_BYTES,
} from '../src/image-attachment.js';

describe('isImagePath', () => {
  it('按扩展名识别图片（大小写不敏感）', () => {
    expect(isImagePath('/a/b.png')).toBe(true);
    expect(isImagePath('/a/b.JPG')).toBe(true);
    expect(isImagePath('/a/b.jpeg')).toBe(true);
    expect(isImagePath('/a/b.webp')).toBe(true);
  });

  it('非图片扩展名返回 false', () => {
    expect(isImagePath('/a/b.txt')).toBe(false);
    expect(isImagePath('/a/b.ts')).toBe(false);
    expect(isImagePath('/a/noext')).toBe(false);
  });
});

describe('computeTargetSize', () => {
  it('未超限保持原尺寸', () => {
    expect(computeTargetSize(800, 600)).toEqual({ width: 800, height: 600, resized: false });
  });

  it('按长边等比缩小（宽图）', () => {
    const r = computeTargetSize(3136, 1568, 1568);
    expect(r.resized).toBe(true);
    expect(r.width).toBe(1568);
    expect(r.height).toBe(784);
  });

  it('按长边等比缩小（高图）', () => {
    const r = computeTargetSize(1000, 4000, 2000);
    expect(r.resized).toBe(true);
    expect(r.height).toBe(2000);
    expect(r.width).toBe(500);
  });

  it('从不上采样', () => {
    expect(computeTargetSize(100, 100, 1568).resized).toBe(false);
  });

  it('非法尺寸返回 0', () => {
    expect(computeTargetSize(0, 100)).toEqual({ width: 0, height: 0, resized: false });
  });
});

describe('parseImageAttachments', () => {
  it('非数组返回空', () => {
    expect(parseImageAttachments(undefined)).toEqual([]);
    expect(parseImageAttachments('x')).toEqual([]);
    expect(parseImageAttachments(null)).toEqual([]);
  });

  it('过滤掉缺 path / 非字符串 path / 非图片扩展', () => {
    const out = parseImageAttachments([
      { path: '/a/ok.png', name: 'ok.png' },
      { path: '/a/skip.txt' },
      { name: 'no-path.png' },
      { path: 123 },
      'not-an-object',
      { path: '/a/ok2.jpg' },
    ]);
    expect(out).toEqual([
      { path: '/a/ok.png', name: 'ok.png' },
      { path: '/a/ok2.jpg', name: undefined },
    ]);
  });
});

describe('processImageAttachments（非 Electron 宿主）', () => {
  it('文件不存在 → 记入说明，不产出图片', () => {
    const r = processImageAttachments([{ path: '/definitely/not/here.png', name: 'here.png' }]);
    expect(r.images).toEqual([]);
    expect(r.notes).toHaveLength(1);
    expect(r.notes[0]).toContain('here.png');
    expect(r.notes[0]).toContain('不存在');
  });

  it('源文件超过字节上限 → 拒绝并说明', () => {
    const dir = mkdtempSync(join(tmpdir(), 'img-attach-'));
    try {
      const big = join(dir, 'big.png');
      writeFileSync(big, Buffer.alloc(IMAGE_MAX_SOURCE_BYTES + 1, 0));
      const r = processImageAttachments([{ path: big, name: 'big.png' }]);
      expect(r.images).toEqual([]);
      expect(r.notes[0]).toContain('超过');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('合法图片路径在无解码器环境下 → 说明而非崩溃', () => {
    const dir = mkdtempSync(join(tmpdir(), 'img-attach-'));
    try {
      const p = join(dir, 'ok.png');
      writeFileSync(p, Buffer.from([137, 80, 78, 71])); // PNG magic, 内容无所谓
      const r = processImageAttachments([{ path: p, name: 'ok.png' }]);
      // vitest 里没有 nativeImage，应走「不支持图片解码」分支
      expect(r.images).toEqual([]);
      expect(r.notes[0]).toContain('ok.png');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
