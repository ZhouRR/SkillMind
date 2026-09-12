import { isNonNilUuid } from './validation'

const RECEIPT_KEY = 'skillmind:interpretation-request:v1'

/** 元 UUID だけを tab 内に保持し、credential や Skill 本文は保存しない。 */
export function saveInterpretationReceipt(requestId: string): void {
  if (!isNonNilUuid(requestId)) throw new Error('Invalid interpretation request identity')
  sessionStorage.setItem(RECEIPT_KEY, requestId)
}

/** 再読不能な値は黙って捨てず、呼出元に観測の不明状態を返す。 */
export function loadInterpretationReceipt(): string | null {
  const value = sessionStorage.getItem(RECEIPT_KEY)
  if (value !== null && !isNonNilUuid(value)) throw new Error('Invalid saved interpretation request')
  return value
}

/** 観測の終了時だけ手元 UUID を消し、実行の取消とは区別する。 */
export function clearInterpretationReceipt(): void {
  sessionStorage.removeItem(RECEIPT_KEY)
}
