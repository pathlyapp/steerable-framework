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

/**
 * 把一批附件持久化到会话附件目录，返回「提交时应使用」的文件列表：
 * 成功项用落盘路径，失败项退回原文件（不让用户的选择因单个文件失败而丢失）。
 * 非 Electron / 无 attachments 桥 / 空 chatId 时原样返回。
 */
export async function saveChatAttachments(
  chatId: string | null | undefined,
  files: AttachmentFile[],
): Promise<AttachmentFile[]> {
  if (!chatId || files.length === 0) return files;
  if (!isElectron()) return files;
  const bridge = getElectronBridge();
  if (!bridge?.attachments?.save) return files;
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
    return files.map((file, index) => {
      const entry = stored[index];
      if (entry && entry.path && !entry.error) {
        return { name: entry.name || file.name, path: entry.path };
      }
      return file;
    });
  } catch (err) {
    console.warn('[attachments] save failed, falling back to source paths', err);
    return files;
  }
}
