/** 画像・文字文書・抜粋に共通する preview 入力の byte 上限（20 MB）。 */
export const DOCUMENT_PREVIEW_MAX_BYTES = 20_000_000

/** 巨大な文字列を複製せず、UTF-8 byte を数えて共通上限で早期終了する。 */
export function previewTextExceedsLimit(source: string): boolean {
  let bytes = 0
  for (let index = 0; index < source.length; index++) {
    const code = source.charCodeAt(index)
    if (code < 0x80) bytes++
    else if (code < 0x800) bytes += 2
    else if (code >= 0xd800 && code <= 0xdbff && index + 1 < source.length
      && source.charCodeAt(index + 1) >= 0xdc00 && source.charCodeAt(index + 1) <= 0xdfff) {
      bytes += 4
      index++
    } else bytes += 3
    if (bytes > DOCUMENT_PREVIEW_MAX_BYTES) return true
  }
  return false
}
