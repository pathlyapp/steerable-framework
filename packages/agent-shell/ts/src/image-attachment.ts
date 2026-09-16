/**
 * W6-3: turn an attached image file into a base64 `LlmImage` the model can
 * actually see, replacing the old behavior of dropping only the file *path*
 * into the prompt text.
 *
 * Runs in the Electron main process and uses `nativeImage` to decode /
 * downscale — no native dependency, works in the packaged app. Two caps are
 * enforced before the image ever reaches the model:
 *
 *   - source bytes  (`IMAGE_MAX_SOURCE_BYTES`) — refuse to read huge files;
 *   - long-edge px  (`IMAGE_MAX_DIMENSION`)    — downscale so the provider's
 *     vision input limit and our token budget aren't blown by a 4K screenshot;
 *   - encoded bytes (`IMAGE_MAX_ENCODED_BYTES`) — re-encode PNG->JPEG and
 *     finally refuse if still too large.
 *
 * Every decision (attached / resized / skipped + why) is returned as a note
 * line so the caller can inject it into the model-visible context — the model
 * should know an image was attached even when it was too large to send.
 */
import { getNativeImage } from './runtime.js';
import { statSync } from 'fs';
import { basename, extname } from 'path';
import type { LlmImage } from './llm/types.js';

/** Refuse to read source files larger than this (10 MB). */
export const IMAGE_MAX_SOURCE_BYTES = 10 * 1024 * 1024;
/** Downscale so the long edge is at most this many px (matches common vision limits). */
export const IMAGE_MAX_DIMENSION = 1568;
/** Refuse the encoded payload if it still exceeds this after re-encode (5 MB). */
export const IMAGE_MAX_ENCODED_BYTES = 5 * 1024 * 1024;

const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp']);

export interface ImageAttachmentInput {
  path: string;
  name?: string;
}

export interface ProcessedImageAttachments {
  images: LlmImage[];
  /** One line per input describing the outcome, for the model-visible note. */
  notes: string[];
}

export function isImagePath(path: string): boolean {
  return IMAGE_EXTENSIONS.has(extname(path).toLowerCase());
}

/**
 * Validate the wire value (`metadata.images` from the renderer) into a clean
 * input list. Anything that isn't an object with a non-empty string `path`
 * is dropped; non-image extensions are dropped here too so a renamed file
 * can't smuggle arbitrary bytes into the decoder.
 */
export function parseImageAttachments(value: unknown): ImageAttachmentInput[] {
  if (!Array.isArray(value)) return [];
  const out: ImageAttachmentInput[] = [];
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const path = (item as Record<string, unknown>).path;
    if (typeof path !== 'string' || !path.trim() || !isImagePath(path)) continue;
    const name = (item as Record<string, unknown>).name;
    out.push({ path, name: typeof name === 'string' ? name : undefined });
  }
  return out;
}

/**
 * Pure resize decision, exported for tests: given source dimensions, return
 * the target dimensions honoring `IMAGE_MAX_DIMENSION` (aspect preserved,
 * never upscale, never below 1px).
 */
export function computeTargetSize(
  width: number,
  height: number,
  maxDimension: number = IMAGE_MAX_DIMENSION,
): { width: number; height: number; resized: boolean } {
  if (width <= 0 || height <= 0) return { width: 0, height: 0, resized: false };
  if (width <= maxDimension && height <= maxDimension) {
    return { width, height, resized: false };
  }
  const scale = Math.min(maxDimension / width, maxDimension / height);
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
    resized: true,
  };
}

function formatMb(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(1).replace(/\.0$/, '');
}

/**
 * Process a batch of image attachments. Synchronous: `nativeImage` decode /
 * resize / encode are all synchronous, and the byte check uses `statSync`.
 */
export function processImageAttachments(
  files: ImageAttachmentInput[],
): ProcessedImageAttachments {
  const images: LlmImage[] = [];
  const notes: string[] = [];

  for (const file of files) {
    const label = file.name || basename(file.path);

    // Existence + source-size guards run before the decoder so they're
    // exercisable in a non-Electron (test) host too.
    let sourceBytes = 0;
    try {
      sourceBytes = statSync(file.path).size;
    } catch {
      notes.push(`- ${label}：文件不存在或不可读，未附加`);
      continue;
    }
    if (sourceBytes > IMAGE_MAX_SOURCE_BYTES) {
      notes.push(`- ${label}：源文件 ${formatMb(sourceBytes)}MB 超过 ${formatMb(IMAGE_MAX_SOURCE_BYTES)}MB 上限，未附加`);
      continue;
    }

    const nativeImage = getNativeImage();
    if (!nativeImage) {
      // Non-Electron host (BS server, unit tests, headless) — can't decode images.
      notes.push(`- ${label}：当前运行环境不支持图片解码，未附加`);
      continue;
    }

    const image = nativeImage.createFromPath(file.path);
    if (image.isEmpty()) {
      notes.push(`- ${label}：不是可识别的图片，未附加`);
      continue;
    }

    const { width, height } = image.getSize();
    const target = computeTargetSize(width, height);
    const rendered = target.resized
      ? image.resize({ width: target.width, height: target.height, quality: 'good' })
      : image;

    // PNG for graphics/screenshots (lossless), JPEG for photos (smaller).
    const preferJpeg = ['.jpg', '.jpeg'].includes(extname(file.path).toLowerCase());
    let mediaType = preferJpeg ? 'image/jpeg' : 'image/png';
    let buffer = preferJpeg ? rendered.toJPEG(85) : rendered.toPNG();
    if (buffer.length > IMAGE_MAX_ENCODED_BYTES && mediaType !== 'image/jpeg') {
      mediaType = 'image/jpeg';
      buffer = rendered.toJPEG(80);
    }
    if (buffer.length > IMAGE_MAX_ENCODED_BYTES) {
      notes.push(`- ${label}：压缩后仍超过 ${formatMb(IMAGE_MAX_ENCODED_BYTES)}MB，未附加`);
      continue;
    }

    images.push({ data: buffer.toString('base64'), mediaType });
    const sizeText = target.resized
      ? `${width}×${height}，已缩放至 ${target.width}×${target.height}`
      : `${width}×${height}`;
    notes.push(`- ${label}（${sizeText}）`);
  }

  return { images, notes };
}
