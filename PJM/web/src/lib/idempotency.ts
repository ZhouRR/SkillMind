/** Idempotency key 生成に必要な Web Crypto の最小 interface。 */
interface CryptoSource {
  randomUUID?: () => string
  getRandomValues?: (array: Uint8Array<ArrayBuffer>) => Uint8Array<ArrayBuffer>
}

/**
 * Run 作成用の UUID v4 を生成する。
 *
 * HTTP の非 secure context では randomUUID が公開されない browser があるため、
 * 同じ Web Crypto の getRandomValues から RFC 4122 の version/variant bit を構築する。
 */
export function createIdempotencyKey(
  cryptoSource: CryptoSource | null | undefined = globalThis.crypto,
): string {
  if (typeof cryptoSource?.randomUUID === 'function') {
    return cryptoSource.randomUUID()
  }
  if (typeof cryptoSource?.getRandomValues !== 'function') {
    throw new Error('This browser does not provide secure random number generation')
  }

  const bytes = cryptoSource.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join('-')
}
