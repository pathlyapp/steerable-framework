/**
 * 会话附件持久化的 renderer 契约：上传的是**文件**（docx/pdf/任意二进制），
 * 图片只是其中一个可选的额外通道。这里钉死三件事：
 *   1. 浏览器模式下 File 没有路径，必须靠落盘结果回填路径——绝不把空路径
 *      写回给调用方（否则消息正文出现空引用，模型以为收到文件却读不到）；
 *   2. 落盘失败时，浏览器模式（无源路径）必须进 `failures` 让用户看到；
 *      Electron 模式（有源路径）才允许退回源路径；
 *   3. 图片判定只影响 `metadata.images`，与「文件能不能持久化」无关。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  appendAttachmentRefs,
  collectImageAttachments,
  fileToBase64,
  formatAttachmentFailures,
  isImageFile,
  saveChatAttachments,
  type AttachmentFile,
} from './attachments';

vi.mock('./electron-bridge', () => ({
  isElectron: vi.fn(),
  getElectronBridge: vi.fn(),
}));

import { getElectronBridge, isElectron } from './electron-bridge';

const isElectronMock = vi.mocked(isElectron);
const getBridgeMock = vi.mocked(getElectronBridge);

const CHAT_ID = 'chat-1';

function browserFile(name: string): AttachmentFile {
  return { name, path: '', file: new File(['hello'], name, { type: 'application/octet-stream' }) };
}

function attachBridge(save: ReturnType<typeof vi.fn>): void {
  isElectronMock.mockReturnValue(true);
  getBridgeMock.mockReturnValue({ attachments: { save } } as never);
}

afterEach(() => {
  vi.clearAllMocks();
});

// 失败路径会 console.warn（预期行为），压掉以免污染测试输出。
beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

describe('saveChatAttachments', () => {
  it('空 chatId / 空列表原样返回，不报失败', async () => {
    isElectronMock.mockReturnValue(true);
    getBridgeMock.mockReturnValue(null);
    const files = [browserFile('a.docx')];

    await expect(saveChatAttachments(null, files)).resolves.toEqual({ files, failures: [] });
    await expect(saveChatAttachments(CHAT_ID, [])).resolves.toEqual({ files: [], failures: [] });
  });

  it('浏览器模式：无路径文件落盘后回填落盘路径（非图片同样持久化）', async () => {
    const save = vi.fn().mockResolvedValue({
      files: [{ name: 'a.docx', path: '/data/attachments/chat-1/a.docx', size: 5 }],
    });
    attachBridge(save);

    const result = await saveChatAttachments(CHAT_ID, [browserFile('a.docx')]);

    expect(save).toHaveBeenCalledTimes(1);
    const payload = save.mock.calls[0][0];
    expect(payload.chatId).toBe(CHAT_ID);
    expect(payload.files[0].path).toBeUndefined();
    expect(typeof payload.files[0].data).toBe('string');
    expect(result.failures).toEqual([]);
    expect(result.files).toEqual([
      { name: 'a.docx', path: '/data/attachments/chat-1/a.docx' },
    ]);
  });

  it('浏览器模式：落盘失败的文件被剔除并上报，绝不留空路径', async () => {
    const save = vi.fn().mockResolvedValue({
      files: [{ name: 'big.bin', path: '', size: 0, error: 'file too large' }],
    });
    attachBridge(save);

    const result = await saveChatAttachments(CHAT_ID, [browserFile('big.bin')]);

    expect(result.files).toEqual([]);
    expect(result.failures).toHaveLength(1);
    expect(result.failures[0]).toMatchObject({ name: 'big.bin', error: 'file too large' });
    // 没有可用路径 → 调用方拿不到任何 path:'' 的项。
    expect(result.files.some((f) => !f.path)).toBe(false);
  });

  it('整批请求失败：浏览器模式全部上报，Electron 保留源路径', async () => {
    const save = vi.fn().mockRejectedValue(new Error('request body too large (max 64MB)'));
    attachBridge(save);

    const browser = await saveChatAttachments(CHAT_ID, [browserFile('a.pdf')]);
    expect(browser.files).toEqual([]);
    expect(browser.failures[0].error).toContain('request body too large');

    const electronFile: AttachmentFile = { name: 'a.pdf', path: '/Users/me/a.pdf' };
    const electron = await saveChatAttachments(CHAT_ID, [electronFile]);
    expect(electron.failures).toEqual([]);
    expect(electron.files).toEqual([electronFile]);
  });

  it('部分成功：成功的用落盘路径，失败的单独上报', async () => {
    const save = vi.fn().mockResolvedValue({
      files: [
        { name: 'ok.csv', path: '/data/attachments/chat-1/ok.csv', size: 3 },
        { name: 'bad.csv', path: '', size: 0, error: 'disk full' },
      ],
    });
    attachBridge(save);

    const result = await saveChatAttachments(CHAT_ID, [browserFile('ok.csv'), browserFile('bad.csv')]);

    expect(result.files).toEqual([{ name: 'ok.csv', path: '/data/attachments/chat-1/ok.csv' }]);
    expect(result.failures.map((f) => f.name)).toEqual(['bad.csv']);
  });
});

describe('isImageFile / formatAttachmentFailures', () => {
  it('只把图片扩展名判为图片，文档类不被图片逻辑吞掉', () => {
    expect(isImageFile('/x/a.PNG')).toBe(true);
    expect(isImageFile('/x/a.jpeg')).toBe(true);
    expect(isImageFile('/x/a.docx')).toBe(false);
    expect(isImageFile('/x/a.pdf')).toBe(false);
    expect(isImageFile('/x/noext')).toBe(false);
  });

  it('失败提示是可读中文且带文件名', () => {
    const msg = formatAttachmentFailures([
      { name: 'a.bin', error: 'file too large', file: browserFile('a.bin') },
      { name: 'b.bin', error: 'disk full', file: browserFile('b.bin') },
    ]);
    expect(msg).toContain('a.bin');
    expect(msg).toContain('b.bin');
    expect(msg).toContain('file too large');
    expect(formatAttachmentFailures([])).toBe('');
  });
});

describe('appendAttachmentRefs / collectImageAttachments（文件优先，图片只是额外通道）', () => {
  it('所有文件都写路径引用，非图片不会被图片逻辑吞掉', () => {
    const content = appendAttachmentRefs('帮我做PPT', [
      { name: 'a.docx', path: '/d/att/a.docx' },
      { name: 'b.pdf', path: '/d/att/b.pdf' },
      { name: 'p.png', path: '/d/att/p.png' },
    ]);
    expect(content.startsWith('帮我做PPT')).toBe(true);
    expect(content).toContain('关联文件:');
    expect(content).toContain('- `/d/att/a.docx`');
    expect(content).toContain('- `/d/att/b.pdf`');
    expect(content).toContain('- `/d/att/p.png`');
  });

  it('只有正文为空时，正文退化为引用段', () => {
    expect(appendAttachmentRefs('', [{ name: 'a.docx', path: '/d/a.docx' }])).toBe(
      '关联文件:\n- `/d/a.docx`',
    );
    expect(appendAttachmentRefs('hi', [])).toBe('hi');
  });

  it('metadata.images 只收图片；文档类只走正文路径', () => {
    const files: AttachmentFile[] = [
      { name: 'p.png', path: '/d/p.png' },
      { name: 'a.docx', path: '/d/a.docx' },
    ];
    expect(collectImageAttachments(files)).toEqual([{ path: '/d/p.png', name: 'p.png' }]);
  });
});

describe('fileToBase64', () => {
  it('小文件逐字节编码', async () => {
    const file = new File([new Uint8Array([1, 2, 3, 255])], 'x.bin');
    expect(await fileToBase64(file)).toBe('AQID/w==');
  });

  it('空文件编码为空串', async () => {
    expect(await fileToBase64(new File([], 'empty'))).toBe('');
  });

  it('跨 0x8000 分块边界的编码与一次性编码一致', async () => {
    const bytes = new Uint8Array(0x8000 + 100).fill(0x61);
    const file = new File([bytes], 'big.bin');
    const expected = Buffer.from(bytes).toString('base64');
    expect(await fileToBase64(file)).toBe(expected);
  });
});
