export interface ContentPart {
  /**
   * Part discriminant. `text` carries `text`; `image` carries either `url` or (`data`, `mediaType`).
   */
  type: "text" | "image";
  /**
   * Text payload; present iff type == "text".
   */
  text?: string;
  /**
   * Image URL; present iff type == "image" and the image is remote.
   */
  url?: string;
  /**
   * Base64 image payload; present iff type == "image" and the image is inline.
   */
  data?: string;
  /**
   * MIME type of the image (e.g. image/png); accompanies `data`, advisory for `url`.
   */
  mediaType?: string;
}
