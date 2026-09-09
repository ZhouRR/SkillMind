/** 公開 UUID の形だけを確認し、生成や所有権の判定は server に任せる。 */
export function isUuid(value: unknown): value is string {
  return typeof value === 'string'
    && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value)
}

/** Python datetime が返す timezone 付き日時を、日付の自動繰上げなしで読む。 */
export function isApiTimestamp(value: unknown): value is string {
  if (typeof value !== 'string') return false
  const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))$/i.exec(value)
  if (!parts) return false
  const [year, month, day, hour, minute, second] = parts.slice(1, 7).map(Number) as [number, number, number, number, number, number]
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1]!
    && hour <= 23 && minute <= 59 && second <= 59
    && (parts[7] === undefined || Number(parts[8]) <= 23 && Number(parts[9]) <= 59)
    && Number.isFinite(Date.parse(value))
}
