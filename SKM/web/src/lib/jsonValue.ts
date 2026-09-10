/** HTTP で意味を失わず表現できる JSON 値だけを受理する。 */
export function isJsonValue(value: unknown, ancestors = new Set<object>()): boolean {
  if (typeof value === 'string') return !/[\uD800-\uDFFF]/u.test(value)
  if (value === null || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (typeof value !== 'object' || ancestors.has(value)) return false
  const array = Array.isArray(value)
  const prototype = Object.getPrototypeOf(value)
  if (array ? prototype !== Array.prototype : prototype !== Object.prototype && prototype !== null) return false
  ancestors.add(value)
  try {
    const keys = Reflect.ownKeys(value)
    if (array && keys.length !== value.length + 1) return false
    return keys.every((key) => {
      if (array && key === 'length') return true
      const descriptor = Object.getOwnPropertyDescriptor(value, key)!
      return typeof key === 'string' && !/[\uD800-\uDFFF]/u.test(key) && descriptor.enumerable && Object.hasOwn(descriptor, 'value')
        && (!array || /^(0|[1-9]\d*)$/.test(key) && Number(key) < value.length)
        && isJsonValue(descriptor.value, ancestors)
    })
  } finally { ancestors.delete(value) }
}

/** Object の key 順だけを無視し、配列順序・数値・bool・null の違いは維持する。 */
export function sameJsonValue(left: unknown, right: unknown): boolean {
  if (left === right) return true
  if (typeof left !== 'object' || left === null || typeof right !== 'object' || right === null) return false
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((item, index) => sameJsonValue(item, right[index]))
  }
  const a = left as Record<string, unknown>
  const b = right as Record<string, unknown>
  return Object.keys(a).length === Object.keys(b).length
    && Object.keys(a).every((key) => Object.hasOwn(b, key) && sameJsonValue(a[key], b[key]))
}
