/**
 * 会话附件空间：把用户在输入框上传的文件持久化到会话对应的空间内。
 *
 * 每个会话一个独立目录：`<userDataDir>/attachments/<chatId>/`。持久化的
 * 好处与场景包的每会话工作区（<pack>-projects/<chatId>）一致：
 *   - 文件不再依赖「原始路径」——用户拖进来的临时下载文件、截图被移动 /
 *     删除后，会话里的引用仍然有效；
 *   - 后续轮次 agent 可以通过 `local_read_file`（以及图像附件的多模态
 *     `metadata.images`）稳定读回这份文件；
 *   - 目录按 chatId 隔离，删除会话时（未来的清理逻辑）可以整体回收。
 *
 * 与 `image-attachment.ts` 的分工：本模块只负责「把文件放进去」；图片怎么
 * 解码成模型可见的 base64 仍由 `image-attachment.ts` 处理。二者路径都在
 * 主进程 / BS 服务端。
 *
 * 写入支持两种来源（Electron 与 BS 模式各用其一）：
 *   - `path`：Electron 拖拽 / `<input type="file">` 的 File 对象带真实路径，
 *     服务端直接 copyFile；
 *   - `data`：浏览器（BS 模式）里 File 没有路径，renderer 读成 base64 传上
 *     来，服务端解码后落盘。
 */
import { existsSync, mkdirSync } from 'node:fs';
import { copyFile, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { getUserDataDir } from './runtime.js';

export const ATTACHMENTS_DIR_NAME = 'attachments';

/** 单个附件上限（base64 解码前字节数）；多文件总和还受 HTTP body 上限约束。 */
export const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;

/** chatId 由 storage 用 randomUUID 生成；这里只做白名单防御。 */
const SAFE_CHAT_ID = /^[0-9a-zA-Z_-]{1,64}$/;

export interface AttachmentInput {
  /** Electron：源文件绝对路径。 */
  path?: string;
  /** 原始文件名（用于落盘命名）。 */
  name?: string;
  /** BS/browser：base64 编码的文件字节。 */
  data?: string;
}

export interface StoredAttachment {
  /** 落盘后的文件名（可能与原名不同，同名会加 -N 后缀）。 */
  name: string;
  /** 落盘后的绝对路径。失败时为空串。 */
  path: string;
  /** 文件字节数。失败时为 0。 */
  size: number;
  /** 单个文件失败原因（成功时缺省）。 */
  error?: string;
}

export function isValidChatAttachmentsKey(chatId: string): boolean {
  return SAFE_CHAT_ID.test(chatId);
}

/**
 * 当前会话的附件目录路径，**不创建目录**（供拼系统提示等只读场景使用）。
 * chatId 非法时仍然返回路径但由调用方在写入前用
 * {@link isValidChatAttachmentsKey} 校验。
 */
export function chatAttachmentsDirPath(chatId: string): string {
  return path.join(getUserDataDir(), ATTACHMENTS_DIR_NAME, chatId);
}

/**
 * 当前会话的附件目录（会确保目录存在）。chatId 非法时仍然返回路径但由
 * 调用方在写入前用 {@link isValidChatAttachmentsKey} 校验。
 */
export function getChatAttachmentsDir(chatId: string): string {
  const dir = chatAttachmentsDirPath(chatId);
  mkdirSync(dir, { recursive: true });
  return dir;
}

/** 去掉路径分隔符与控制字符，只保留安全的文件名。 */
export function sanitizeName(name: string): string {
  const raw = String(name ?? '').replace(/\\/g, '/');
  const base = raw.split('/').pop() ?? '';
  const cleaned = base
    // 控制字符与 Windows/跨平台保留字符统一替换为下划线。
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u001f<>:"|?*]/g, '_')
    .trim();
  return cleaned || 'file';
}

/** 同名文件加 -1 / -2 后缀，避免覆盖已有附件。 */
export function uniqueFileName(dir: string, name: string): string {
  if (!existsSync(path.join(dir, name))) return name;
  const ext = path.extname(name);
  const base = path.basename(name, ext);
  for (let i = 1; i < 100_000; i += 1) {
    const candidate = `${base}-${i}${ext}`;
    if (!existsSync(path.join(dir, candidate))) return candidate;
  }
  return `${base}-${Date.now()}${ext}`;
}

/** base64 → Buffer；空串返回 null（表示「没有数据来源」）。 */
function decodeData(data: string): Buffer | null {
  const text = data.trim();
  if (!text) return null;
  return Buffer.from(text, 'base64');
}

/**
 * 把一批附件落进会话附件目录。按输入顺序返回逐文件结果（成功或失败都
 * 不会中断整批），调用方据此决定哪些文件可用。
 *
 * 每个文件二选一提供来源：`path`（服务端直接拷贝）或 `data`（base64 字节，
 * 服务端解码落盘）。两者都缺时记 error；两者都有时优先 `data`。
 */
export async function saveAttachmentFiles(
  chatId: string,
  files: AttachmentInput[],
): Promise<{ files: StoredAttachment[] }> {
  const out: StoredAttachment[] = [];

  if (!isValidChatAttachmentsKey(chatId)) {
    for (const file of files) {
      const name = sanitizeName(file.name || path.basename(file.path || ''));
      out.push({ name, path: '', size: 0, error: `invalid chatId: ${chatId}` });
    }
    return { files: out };
  }

  const dir = getChatAttachmentsDir(chatId);
  for (const file of files) {
    const sourcePath = typeof file.path === 'string' ? file.path.trim() : '';
    const name = sanitizeName(file.name || path.basename(sourcePath || ''));
    const data = typeof file.data === 'string' ? file.data : '';

    try {
      if (data.trim()) {
        const bytes = decodeData(data) as Buffer;
        const size = bytes.length;
        if (size > ATTACHMENT_MAX_BYTES) {
          out.push({
            name,
            path: '',
            size: 0,
            error: `file too large (${size} bytes), max=${ATTACHMENT_MAX_BYTES}`,
          });
          continue;
        }
        const storedName = uniqueFileName(dir, name);
        const destPath = path.join(dir, storedName);
        await writeFile(destPath, bytes);
        out.push({ name: storedName, path: destPath, size });
        continue;
      }

      if (sourcePath) {
        const sourceStat = await stat(sourcePath);
        if (!sourceStat.isFile()) {
          out.push({ name, path: '', size: 0, error: 'source is not a regular file' });
          continue;
        }
        if (sourceStat.size > ATTACHMENT_MAX_BYTES) {
          out.push({
            name,
            path: '',
            size: 0,
            error: `file too large (${sourceStat.size} bytes), max=${ATTACHMENT_MAX_BYTES}`,
          });
          continue;
        }
        const storedName = uniqueFileName(dir, name);
        const destPath = path.join(dir, storedName);
        await copyFile(sourcePath, destPath);
        out.push({ name: storedName, path: destPath, size: sourceStat.size });
        continue;
      }

      out.push({ name, path: '', size: 0, error: 'no content (missing path and data)' });
    } catch (err) {
      out.push({
        name,
        path: '',
        size: 0,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }
  return { files: out };
}
