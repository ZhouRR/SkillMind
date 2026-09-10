/** 原 JSON の数値 token を binary64 への暗黙変換から保護する共通 codec。 */
declare const numberBrand: unique symbol

/** 値の信頼性は公開 field でなく module 内 WeakMap の登録で確認する。 */
export interface LosslessJsonNumber {
  readonly raw: string
  readonly [numberBrand]: true
}

/** 整数は十進 token、浮動小数は Python wire と同じ binary64 として比較する。 */
interface NumberToken { readonly raw: string; readonly integer: boolean; readonly float: number | null }

const numbers = new WeakMap<object, NumberToken>()
const MAX_DEPTH = 64

/** 元本文・値を例外へ混ぜず、呼出元が同じ契約失敗として処理できるようにする。 */
function invalidJson(): never { throw new SyntaxError('Invalid lossless JSON') }

/** JSON.stringify の偶発的な利用は、空 object や丸めた値を返さず明示的に止める。 */
function rejectNativeSerialization(): never { throw new TypeError('Use stringifyLosslessJson for lossless numbers') }

/** 巨大整数を Number/BigInt へ変換せず、元 token を immutable に保持する。 */
function numberToken(raw: string): number | LosslessJsonNumber {
  const integer = !/[.eE]/.test(raw)
  if (integer && raw !== '-0' && raw.length <= 17) {
    const value = Number(raw)
    if (Number.isSafeInteger(value)) return value
  }
  const value: object = Object.create(null)
  Object.defineProperties(value, {
    raw: { value: raw, enumerable: true },
    toJSON: { value: rejectNativeSerialization },
  })
  numbers.set(value, Object.freeze({ raw, integer, float: integer ? null : Number(raw) }))
  return Object.freeze(value) as LosslessJsonNumber
}

/** 原型や raw property を調べず、getter と brand の偽造を受け入れない。 */
export function isLosslessJsonNumber(value: unknown): value is LosslessJsonNumber {
  return typeof value === 'object' && value !== null && numbers.has(value)
}

/** JSON の有限 float と任意桁の整数を認め、boolean や overflow float は拒否する。 */
export function isJsonNumeric(value: unknown): boolean {
  if (typeof value === 'number') return Number.isFinite(value)
  if (!isLosslessJsonNumber(value)) return false
  const token = numbers.get(value)!
  return token.integer || Number.isFinite(token.float)
}

/** 原 compiler の integer は int token のみであり、1.0/1e0 を昇格させない。 */
export function isJsonIntegerToken(value: unknown): boolean {
  if (typeof value === 'number') return Number.isSafeInteger(value)
  return isLosslessJsonNumber(value) && numbers.get(value)!.integer
}

/** Python int/float の等値に合わせ、丸め前の異なる整数 enum を併合しない。 */
export function jsonNumericKey(value: unknown): string {
  if (!isJsonNumeric(value)) throw new TypeError('Expected a finite JSON number')
  if (isLosslessJsonNumber(value)) {
    const token = numbers.get(value)!
    if (token.integer) return `i:${token.raw === '-0' ? '0' : token.raw}`
    value = token.float
  }
  const numeric = value as number
  // 整値 float の BigInt 化は binary64 の実値を表す。1e100 を十進 10**100 と見なさない。
  return Number.isInteger(numeric) ? `i:${BigInt(numeric)}` : `f:${numeric.toString()}`
}

/** 正規整数の符号・桁数・辞書順だけを使い、原 bigint の変換コストを発生させない。 */
function compareIntegers(left: string, right: string): -1 | 0 | 1 {
  if (left === right) return 0
  const negative = left.startsWith('-')
  if (negative !== right.startsWith('-')) return negative ? -1 : 1
  const magnitude = left.length === right.length ? (left < right ? -1 : 1) : (left.length < right.length ? -1 : 1)
  return negative ? (magnitude === -1 ? 1 : -1) : magnitude
}

/** int と非整値 float は、整数部が同じ時だけ小数の符号を比較すれば原値を保てる。 */
function compareIntegerToFraction(integer: string, fraction: number): -1 | 0 | 1 {
  const truncated = BigInt(Math.trunc(fraction)).toString()
  const order = compareIntegers(integer, truncated)
  return order !== 0 ? order : fraction > 0 ? -1 : 1
}

/** 原 compiler の minimum/maximum 比較を、型変換や bigint の丸めなしで再現する。 */
export function compareJsonNumeric(left: unknown, right: unknown): -1 | 0 | 1 {
  const leftKey = jsonNumericKey(left)
  const rightKey = jsonNumericKey(right)
  if (leftKey === rightKey) return 0
  const leftInteger = leftKey.startsWith('i:')
  const rightInteger = rightKey.startsWith('i:')
  if (leftInteger && rightInteger) return compareIntegers(leftKey.slice(2), rightKey.slice(2))
  if (leftInteger) return compareIntegerToFraction(leftKey.slice(2), Number(rightKey.slice(2)))
  if (rightInteger) {
    const order = compareIntegerToFraction(rightKey.slice(2), Number(leftKey.slice(2)))
    return order === -1 ? 1 : order === 1 ? -1 : 0
  }
  return Number(leftKey.slice(2)) < Number(rightKey.slice(2)) ? -1 : 1
}

/** 依存や新しい JSON reviver API を使わず、標準 JSON 文法だけを読み取る。 */
class JsonReader {
  private position = 0

  /** HTTP の byte 上限とは別に、ここでは文法・深度・数値の保真を担当する。 */
  constructor(private readonly text: string) {}

  /** 一つの値の後に残った token と、BOM を含む非 JSON 空白を拒否する。 */
  read(): unknown {
    const value = this.value(0)
    this.whitespace()
    if (this.position !== this.text.length) invalidJson()
    return value
  }

  /** JSON が認める四文字だけを空白として読む。 */
  private whitespace(): void {
    while (' \t\r\n'.includes(this.text[this.position] ?? '\0')) this.position++
  }

  /** 全 container の深さを制限し、Flow envelope を含む正規 payload を許容する。 */
  private value(depth: number): unknown {
    this.whitespace()
    const first = this.text[this.position]
    if (first === '"') return this.string()
    if (first === '-' || this.digit(first)) return this.number()
    for (const [literal, result] of [['true', true], ['false', false], ['null', null]] as const) {
      if (this.text.startsWith(literal, this.position)) { this.position += literal.length; return result }
    }
    if (depth >= MAX_DEPTH) invalidJson()
    if (first === '[') return this.array(depth + 1)
    if (first === '{') return this.object(depth + 1)
    return invalidJson()
  }

  /** 原文の chunk を維持し、escape だけを標準 JSON 規則に従って復元する。 */
  private string(): string {
    this.position++
    let start = this.position
    const chunks: string[] = []
    while (this.position < this.text.length) {
      const character = this.text[this.position++]!
      if (character === '"') { chunks.push(this.text.slice(start, this.position - 1)); return chunks.join('') }
      if (character.charCodeAt(0) < 0x20) invalidJson()
      if (character !== '\\') continue
      chunks.push(this.text.slice(start, this.position - 1))
      const escaped = this.text[this.position++]
      if (escaped === 'u') {
        const hex = this.text.slice(this.position, this.position + 4)
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) invalidJson()
        chunks.push(String.fromCharCode(Number.parseInt(hex, 16)))
        this.position += 4
      } else {
        const escapes: Record<string, string> = { '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t' }
        if (escaped === undefined || !Object.hasOwn(escapes, escaped)) invalidJson()
        chunks.push(escapes[escaped]!)
      }
      start = this.position
    }
    return invalidJson()
  }

  /** ASCII digit のみを受け入れ、Unicode の数字を wire number に解釈しない。 */
  private digit(value: string | undefined): boolean { return value !== undefined && value >= '0' && value <= '9' }

  /** 必須の数字列を読む。指数・小数点の直後が空の場合は受理しない。 */
  private digits(): void {
    const start = this.position
    while (this.digit(this.text[this.position])) this.position++
    if (this.position === start) invalidJson()
  }

  /** 先頭ゼロ・不完全な指数・末尾 token は通常の JSON と同様に拒否する。 */
  private number(): number | LosslessJsonNumber {
    const start = this.position
    if (this.text[this.position] === '-') this.position++
    if (this.text[this.position] === '0') this.position++
    else this.digits()
    if (this.text[this.position] === '.') { this.position++; this.digits() }
    if (this.text[this.position] === 'e' || this.text[this.position] === 'E') {
      this.position++
      if (this.text[this.position] === '+' || this.text[this.position] === '-') this.position++
      this.digits()
    }
    return numberToken(this.text.slice(start, this.position))
  }

  /** 配列の順序を保ち、trailing comma や hole を補完しない。 */
  private array(depth: number): unknown[] {
    this.position++
    this.whitespace()
    const result: unknown[] = []
    if (this.text[this.position] === ']') { this.position++; return result }
    while (true) {
      result.push(this.value(depth))
      this.whitespace()
      const next = this.text[this.position++]
      if (next === ']') return result
      if (next !== ',') invalidJson()
    }
  }

  /** 解読後の重複 key を拒否し、__proto__ を安全な own data property として保持する。 */
  private object(depth: number): Record<string, unknown> {
    this.position++
    this.whitespace()
    const result: Record<string, unknown> = Object.create(null)
    if (this.text[this.position] === '}') { this.position++; return result }
    while (true) {
      this.whitespace()
      if (this.text[this.position] !== '"') invalidJson()
      const key = this.string()
      if (Object.hasOwn(result, key)) invalidJson()
      this.whitespace()
      if (this.text[this.position++] !== ':') invalidJson()
      result[key] = this.value(depth)
      this.whitespace()
      const next = this.text[this.position++]
      if (next === '}') return result
      if (next !== ',') invalidJson()
    }
  }
}

/** 原数値を保持する。本文/token 長は切り詰めず、受信総量の方針は HTTP caller が持つ。 */
export function parseLosslessJson(text: string): unknown {
  if (typeof text !== 'string') invalidJson()
  return new JsonReader(text).read()
}

/** 値の変換、getter、custom toJSON を実行せず、信頼済み数値と JSON data だけを描画する。 */
export function stringifyLosslessJson(value: unknown, space = 0): string {
  if (typeof space !== 'number' || !Number.isFinite(space)) throw new TypeError('Invalid JSON indentation')
  const indent = ' '.repeat(Math.min(10, Math.max(0, Math.trunc(space))))
  const ancestors = new Set<object>()

  /** 同一 object の再利用は許し、祖先への循環だけを拒否する。 */
  function serialize(item: unknown, depth: number): string {
    if (isLosslessJsonNumber(item)) return numbers.get(item)!.raw
    if (item === null) return 'null'
    if (typeof item === 'string' || typeof item === 'boolean') return JSON.stringify(item)
    if (typeof item === 'number' && Number.isFinite(item)) return Object.is(item, -0) ? '-0' : JSON.stringify(item)
    if (typeof item !== 'object' || item === null || depth >= MAX_DEPTH || ancestors.has(item)) throw new TypeError('Invalid lossless JSON value')
    const array = Array.isArray(item)
    const prototype = Object.getPrototypeOf(item)
    if (array ? prototype !== Array.prototype : prototype !== Object.prototype && prototype !== null) throw new TypeError('Invalid lossless JSON prototype')
    const descriptors = Object.getOwnPropertyDescriptors(item)
    const keys = Reflect.ownKeys(descriptors)
    if (keys.some((key) => typeof key !== 'string')) throw new TypeError('Invalid lossless JSON property')
    for (const key of keys as string[]) {
      const descriptor = descriptors[key]!
      if (!Object.hasOwn(descriptor, 'value') || (!descriptor.enumerable && !(array && key === 'length'))) throw new TypeError('Invalid lossless JSON property')
    }
    ancestors.add(item)
    try {
      let entries: string[]
      if (array) {
        const length: unknown = descriptors.length?.value
        if (typeof length !== 'number' || !Number.isSafeInteger(length) || length < 0 || keys.length !== length + 1) throw new TypeError('Invalid lossless JSON array')
        entries = []
        for (let index = 0; index < length; index++) {
          const descriptor = descriptors[index]
          if (!descriptor) throw new TypeError('Invalid lossless JSON array')
          entries.push(serialize(descriptor.value, depth + 1))
        }
      } else {
        entries = (keys as string[]).map((key) => `${JSON.stringify(key)}:${indent ? ' ' : ''}${serialize(descriptors[key]!.value, depth + 1)}`)
      }
      const [open, close] = array ? ['[', ']'] : ['{', '}']
      return entries.length === 0 ? `${open}${close}` : indent
        ? `${open}\n${indent.repeat(depth + 1)}${entries.join(`,\n${indent.repeat(depth + 1)}`)}\n${indent.repeat(depth)}${close}`
        : `${open}${entries.join(',')}${close}`
    } finally { ancestors.delete(item) }
  }
  return serialize(value, 0)
}
