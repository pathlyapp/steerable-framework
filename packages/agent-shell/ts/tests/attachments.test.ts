import { mkdtemp, mkdir, writeFile, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  getChatAttachmentsDir,
  isValidChatAttachmentsKey,
  saveAttachmentFiles,
} from '../src/attachments';

const CHAT_ID = '7bcedaf5-dfba-4cf7-8412-cbb4eb863718';

let tmp: string;
let dataDir: string;
let sourceDir: string;

beforeEach(async () => {
  tmp = await mkdtemp(path.join(tmpdir(), 'attachments-'));
  dataDir = path.join(tmp, 'data');
  sourceDir = path.join(tmp, 'source');
  process.env.DEEPPATH_USER_DATA_DIR = dataDir;
  await mkdir(sourceDir, { recursive: true });
});

afterEach(async () => {
  delete process.env.DEEPPATH_USER_DATA_DIR;
  await rm(tmp, { recursive: true, force: true });
});

describe('attachments / 会话附件空间', () => {
  it('getChatAttachmentsDir 落在 userData/attachments/<chatId> 下并确保目录存在', () => {
    const dir = getChatAttachmentsDir(CHAT_ID);
    expect(dir).toBe(path.join(dataDir, 'attachments', CHAT_ID));
  });

  it('chatId 白名单：UUID 合法，路径穿越/空串非法', () => {
    expect(isValidChatAttachmentsKey(CHAT_ID)).toBe(true);
    expect(isValidChatAttachmentsKey('')).toBe(false);
    expect(isValidChatAttachmentsKey('../escape')).toBe(false);
    expect(isValidChatAttachmentsKey('a'.repeat(65))).toBe(false);
  });

  it('saveAttachmentFiles 把源文件拷贝进会话目录并返回落盘路径', async () => {
    await writeFile(path.join(sourceDir, 'a.txt'), 'hello', 'utf8');
    const { files } = await saveAttachmentFiles(CHAT_ID, [
      { path: path.join(sourceDir, 'a.txt'), name: 'a.txt' },
    ]);
    expect(files).toHaveLength(1);
    expect(files[0].error).toBeUndefined();
    expect(files[0].name).toBe('a.txt');
    expect(files[0].path).toBe(path.join(dataDir, 'attachments', CHAT_ID, 'a.txt'));
    expect(files[0].size).toBe(5);
    await expect(readFile(files[0].path, 'utf8')).resolves.toBe('hello');
  });

  it('同名文件加 -N 后缀不覆盖', async () => {
    await writeFile(path.join(sourceDir, 'a.txt'), 'first', 'utf8');
    await writeFile(path.join(sourceDir, 'b.txt'), 'second', 'utf8');
    const { files } = await saveAttachmentFiles(CHAT_ID, [
      { path: path.join(sourceDir, 'a.txt'), name: 'a.txt' },
      { path: path.join(sourceDir, 'b.txt'), name: 'a.txt' },
    ]);
    expect(files.map((f) => f.name)).toEqual(['a.txt', 'a-1.txt']);
    await expect(readFile(files[0].path, 'utf8')).resolves.toBe('first');
    await expect(readFile(files[1].path, 'utf8')).resolves.toBe('second');
  });

  it('文件名含非法字符时被清洗，源文件缺失时返回 error 且不中断整批', async () => {
    await writeFile(path.join(sourceDir, 'ok.txt'), 'ok', 'utf8');
    const { files } = await saveAttachmentFiles(CHAT_ID, [
      { path: path.join(sourceDir, 'ok.txt'), name: '../bad|name?.txt' },
      { path: path.join(sourceDir, 'missing.txt'), name: 'missing.txt' },
    ]);
    expect(files).toHaveLength(2);
    expect(files[0].error).toBeUndefined();
    expect(files[0].name).not.toContain('..');
    expect(files[0].name).not.toContain('|');
    expect(files[1].error).toBeTruthy();
    expect(files[1].path).toBe('');
  });

  it('非法 chatId 时逐文件返回 error，不写盘', async () => {
    const { files } = await saveAttachmentFiles('../escape', [
      { path: path.join(sourceDir, 'a.txt'), name: 'a.txt' },
    ]);
    expect(files[0].error).toContain('invalid chatId');
    expect(files[0].path).toBe('');
  });

  it('BS 模式：data（base64 字节）直接落盘，无需源路径', async () => {
    const { files } = await saveAttachmentFiles(CHAT_ID, [
      { name: 'b.txt', data: Buffer.from('hello bs', 'utf8').toString('base64') },
    ]);
    expect(files).toHaveLength(1);
    expect(files[0].error).toBeUndefined();
    expect(files[0].name).toBe('b.txt');
    expect(files[0].path).toBe(path.join(dataDir, 'attachments', CHAT_ID, 'b.txt'));
    expect(files[0].size).toBe(8);
    await expect(readFile(files[0].path, 'utf8')).resolves.toBe('hello bs');
  });

  it('data 与 path 都缺失时返回 error，不落盘', async () => {
    const { files } = await saveAttachmentFiles(CHAT_ID, [{ name: 'x.txt' }]);
    expect(files[0].error).toContain('no content');
    expect(files[0].path).toBe('');
  });
});
