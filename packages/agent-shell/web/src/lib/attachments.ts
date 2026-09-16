/**
 * 会话附件（文件上传）的 renderer 侧助手。
 *
 * ChatInput 只负责「收集」文件——通过隐藏的 `<input type="file" multiple>`
 * 或拖拽拿到 File，产出 `AttachmentFile`（{name, path, file?}）交给父组件。
 * 真正把文件落进会话空间（`<userDataDir>/attachments/<chatId>/`）由
 * `saveChatAttachments` 在提交时完成：
 *   - Electron：File 对象带真实 `path`，走 `attachments:save` IPC 拷贝；
 *   - BS/browser：File 没有路径，这里把字节读成 base64 走 HTTP 端点落盘。
 * 返回的落盘路径会重写消息正文里的文件引用与图像附件元数据。
 */
import { getElectronBridge, isElectron } from './electron-bridge';

export interface AttachmentFile {
  name: string;
  /** Electron 下的源绝对路径；浏览器模式为空串（用 file 读字节）。 */
  path: string;
  /** 原始 File 对象，用于浏览器模式在提交时读取字节。 */
  file?: File;
}

const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp']);

/** 扩展名是否为图像（renderer 侧预过滤，后端 image-attachment 会二次校验）。 */
export function isImageFile(path: string): boolean {
  const dot = path.lastIndexOf('.');
  return dot >= 0 && IMAGE_EXTENSIONS.has(path.slice(dot).toLowerCase());
}

/** File 字节 → base64（浏览器模式上传用）。分块避免大文件触发调用栈限制。 */
export async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  const chunkSize = 0x8000;
  let binary = '';
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

/** 单个附件落盘失败的原因（附带原文件，调用方可保留在选择器里让用户重试）。 */
export interface AttachmentSaveFailure {
  name: string;
  error: string;
  /** 失败前的原始选择项；浏览器模式下 `file` 仍在，可重试。 */
  file: AttachmentFile;
}

export interface SaveChatAttachmentsResult {
  /**
   * 本条消息可用的附件：已落盘（返回落盘绝对路径）的项；Electron 下若落盘
   * 失败但源路径仍在，也保留源路径。**不会**再出现浏览器模式那种
   * `path: ''` 的空引用。
   */
  files: AttachmentFile[];
  /** 无法持久化、也没有任何可用路径的文件——调用方必须让用户看到。 */
  failures: AttachmentSaveFailure[];
}

/**
 * 把一批附件持久化到会话附件目录，返回「提交时应使用」的文件列表 + 失败项：
 *   - 成功项用落盘路径（稳定、可被 agent 的 local_read_file 读回）；
 *   - 失败项：Electron 下源路径还可用就退回源路径；浏览器模式下文件没有
 *     路径，落盘失败即不可用，从 `files` 剔除并进 `failures`（避免把
 *     空路径写进消息、让模型以为「收到了文件」却什么都读不到）。
 *
 * 非 Electron / 无 attachments 桥 / 空 chatId 时不做持久化，原样返回
 * （`failures` 为空——此时是调用方自己决定不落盘，不算失败）。
 */
export async function saveChatAttachments(
  chatId: string | null | undefined,
  files: AttachmentFile[],
): Promise<SaveChatAttachmentsResult> {
  if (!chatId || files.length === 0) return { files, failures: [] };
  if (!isElectron()) return { files, failures: [] };
  const bridge = getElectronBridge();
  if (!bridge?.attachments?.save) return { files, failures: [] };
  try {
    // 有真实路径走 path 拷贝（Electron）；没有则读字节走 data 上传（BS/browser）。
    const payloadFiles = await Promise.all(
      files.map(async (file) => {
        if (file.path) return { name: file.name, path: file.path };
        if (file.file) return { name: file.name, data: await fileToBase64(file.file) };
        return { name: file.name, path: file.path || '' };
      }),
    );
    const result = await bridge.attachments.save({ chatId, files: payloadFiles });
    const stored = Array.isArray(result?.files) ? result.files : [];
    // 服务端按输入顺序返回逐文件结果，这里按下标对齐回填。
    const kept: AttachmentFile[] = [];
    const failures: AttachmentSaveFailure[] = [];
    files.forEach((file, index) => {
      const entry = stored[index];
      if (entry && entry.path && !entry.error) {
        kept.push({ name: entry.name || file.name, path: entry.path });
        return;
      }
      if (file.path) {
        // Electron 源路径仍可用，退回源路径（与旧行为一致）。
        kept.push(file);
        return;
      }
      failures.push({ name: file.name, error: entry?.error || '上传失败', file });
    });
    return { files: kept, failures };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.warn('[attachments] save failed, falling back to source paths', err);
    const kept = files.filter((f) => Boolean(f.path));
    const failures = files
      .filter((f) => !f.path)
      .map((f) => ({ name: f.name, error: message, file: f }));
    return { files: kept, failures };
  }
}

/** 把失败项拼成一条给用户看的中文提示（供各提交入口复用）。 */
export function formatAttachmentFailures(failures: AttachmentSaveFailure[]): string {
  if (failures.length === 0) return '';
  const detail = failures.map((f) => `${f.name}（${f.error}）`).join('；');
  return failures.length === 1
    ? `文件「${failures[0].name}」未能上传，本条消息未包含它：${failures[0].error}`
    : `${failures.length} 个文件未能上传，本条消息未包含它们：${detail}`;
}

/**
 * 把附件引用段落拼进用户消息正文。**所有文件**（docx / pdf / xlsx / 任意
 * 二进制，不只是图片）都写落盘绝对路径，agent 用 `local_read_file` 读回；
 * 图片的多模态通道是**额外**的，见 {@link collectImageAttachments}。
 * 两个提交入口（会话内 / 落地页）共用本函数，避免装配规则再次分叉。
 */
export function appendAttachmentRefs(rawText: string, files: AttachmentFile[]): string {
  if (files.length === 0) return rawText;
  const refs = files.map((f) => `- \`${f.path}\``).join('\n');
  return rawText
    ? `${rawText}\n\n---\n关联文件:\n${refs}`
    : `关联文件:\n${refs}`;
}

/**
 * 图片附件的多模态元数据（`metadata.images`）。非图片文件不进这里——它们
 * 已经通过 {@link appendAttachmentRefs} 的路径引用交给 agent 读取，不会被
 * 图片逻辑过滤掉。
 */
export function collectImageAttachments(
  files: AttachmentFile[],
): Array<{ path: string; name: string }> {
  return files
    .filter((f) => isImageFile(f.path))
    .map((f) => ({ path: f.path, name: f.name }));
}
