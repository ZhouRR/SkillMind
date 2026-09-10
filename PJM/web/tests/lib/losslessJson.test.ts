import { describe, expect, it, vi } from 'vitest'

import {
  compareJsonNumeric,
  isJsonIntegerToken,
  isJsonNumeric,
  isLosslessJsonNumber,
  jsonNumericKey,
  parseLosslessJson,
  stringifyLosslessJson,
} from '../../src/lib/losslessJson'

/** 数値 fixture は JS literal の事前丸めを避け、必ず wire text から作る。 */
function numeric(text: string): unknown { return parseLosslessJson(text) }

/** 小さい固定 seed で JSON 文法の組合せを再現し、CI で実行量が増えないようにする。 */
function grammarSamples(): string[] {
  let state = 0x73a19c2d
  const random = (maximum: number): number => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0
    return state % maximum
  }
  const scalars = ['null', 'true', 'false', '0', '-0', '1.0', '0.00001', '-12e+3', '1e9999', '9007199254740993',
    JSON.stringify('中文😀\r\n\t\0\\"/'), JSON.stringify('\ud800'), '"\\u0061\\u0042"']
  const build = (depth: number): string => {
    const kind = depth >= 4 ? 0 : random(3)
    if (kind === 0) return scalars[random(scalars.length)]!
    const values = Array.from({ length: random(4) }, (_, index) => (
      kind === 1 ? build(depth + 1) : `${JSON.stringify(`${index}_属性`)}: ${build(depth + 1)}`
    ))
    return kind === 1 ? `[ ${values.join(',\n')} ]` : `{ ${values.join(',\t')} }`
  }
  return Array.from({ length: 512 }, () => build(0))
}

describe('lossless JSON number tokens', () => {
  it.each(['0', '1', '-1', '9007199254740991', '-9007199254740991'])('keeps safe integer %s as a number', (raw) => {
    const value = numeric(raw)
    expect(typeof value).toBe('number')
    expect(isJsonNumeric(value)).toBe(true)
    expect(isJsonIntegerToken(value)).toBe(true)
    expect(stringifyLosslessJson(value)).toBe(raw)
  })

  it.each(['9007199254740992', '9007199254740993', '-9007199254740993', '-0', '1.0', '1e0', '1E+02', '0.0001', '-0.0', '1e100', '1.7976931348623157e308', '5e-324'])('keeps original token %s immutable', (raw) => {
    const value = numeric(raw)
    expect(isLosslessJsonNumber(value)).toBe(true)
    if (!isLosslessJsonNumber(value)) throw new Error('Expected a lossless token')
    expect(Object.isFrozen(value)).toBe(true)
    expect(value.raw).toBe(raw)
    expect(isJsonNumeric(value)).toBe(true)
    expect(isJsonIntegerToken(value)).toBe(!/[.eE]/.test(raw))
    expect(stringifyLosslessJson(value)).toBe(raw)
    expect(() => JSON.stringify(value)).toThrow('Use stringifyLosslessJson')
    expect(() => JSON.stringify({ value })).toThrow('Use stringifyLosslessJson')
    expect(() => Object.defineProperty(value, 'raw', { value: '1' })).toThrow()
  })

  it('does not convert arbitrarily long integer tokens through Number or BigInt', () => {
    const raw = `-${'9'.repeat(20_000)}`
    const converted = vi.fn()
    const numberConstructor = globalThis.Number
    const integerConstructor = globalThis.BigInt
    vi.stubGlobal('Number', new Proxy(numberConstructor, { apply: (target, receiver, argumentsList) => {
      converted()
      return Reflect.apply(target, receiver, argumentsList)
    } }))
    vi.stubGlobal('BigInt', new Proxy(integerConstructor, { apply: (target, receiver, argumentsList) => {
      converted()
      return Reflect.apply(target, receiver, argumentsList)
    } }))
    try {
      const value = numeric(raw)
      expect(isJsonNumeric(value)).toBe(true)
      expect(isJsonIntegerToken(value)).toBe(true)
      expect(jsonNumericKey(value)).toBe(`i:${raw}`)
      expect(stringifyLosslessJson(value)).toBe(raw)
      expect(converted).not.toHaveBeenCalled()
    } finally {
      vi.stubGlobal('Number', numberConstructor)
      vi.stubGlobal('BigInt', integerConstructor)
    }
  })

  it.each(['1e309', '-1e9999', '1.7976931348623159e308'])('preserves JSON grammar %s but refuses it as a finite numeric value', (raw) => {
    const value = numeric(raw)
    expect(isLosslessJsonNumber(value)).toBe(true)
    expect(isJsonNumeric(value)).toBe(false)
    expect(isJsonIntegerToken(value)).toBe(false)
    expect(() => jsonNumericKey(value)).toThrow(TypeError)
    expect(stringifyLosslessJson(value)).toBe(raw)
  })

  it.each(['1.0', '1e0', '-0.0', '9007199254740992.0'])('does not reclassify float %s as an integer enum token', (raw) => {
    expect(isJsonNumeric(numeric(raw))).toBe(true)
    expect(isJsonIntegerToken(numeric(raw))).toBe(false)
  })

  it.each([true, false, null, undefined, '1', 1n, NaN, Infinity, -Infinity, {}, { raw: '1' }])('does not grant numeric identity to %s', (value) => {
    expect(isJsonNumeric(value)).toBe(false)
    expect(isJsonIntegerToken(value)).toBe(false)
    expect(() => jsonNumericKey(value)).toThrow(TypeError)
  })

  it('uses private identity rather than readable fields, inherited prototypes, proxies or cloned brands', () => {
    const original = numeric('9007199254740993')
    if (!isLosslessJsonNumber(original)) throw new Error('Expected a lossless token')
    const getter = vi.fn(() => original.raw)
    const spoof = Object.defineProperty({}, 'raw', { get: getter })
    for (const value of [spoof, { ...original }, Object.create(original), new Proxy(original, {}), structuredClone(original)]) {
      expect(isLosslessJsonNumber(value)).toBe(false)
      expect(isJsonNumeric(value)).toBe(false)
      expect(isJsonIntegerToken(value)).toBe(false)
    }
    expect(getter).not.toHaveBeenCalled()
  })
})

describe('Python wire numeric equality', () => {
  it.each([
    ['1', '1.0'], ['1', '1e0'], ['0', '-0'], ['0', '-0.0'], ['0', '-1e-9999'],
    ['0.1', '0.10000000000000001'], ['9007199254740992', '9007199254740992.0'],
    ['9007199254740992', '9007199254740993.0'], ['-9007199254740992', '-9007199254740993.0'],
  ])('matches Python equality for %s and %s', (left, right) => {
    expect(jsonNumericKey(numeric(left))).toBe(jsonNumericKey(numeric(right)))
    expect(compareJsonNumeric(numeric(left), numeric(right))).toBe(0)
  })

  it.each([
    ['9007199254740992', '9007199254740993'], ['-9007199254740992', '-9007199254740993'],
    ['9007199254740993', '9007199254740992.0'], ['0', '5e-324'], ['0.1', '0.10000000000000002'],
    [`1${'0'.repeat(100)}`, '1e100'],
  ])('retains distinct Python values %s and %s', (left, right) => {
    expect(jsonNumericKey(numeric(left))).not.toBe(jsonNumericKey(numeric(right)))
  })

  it('compares a binary64 integer using its actual integer value, not its shortest decimal spelling', () => {
    const actual = '10000000000000000159028911097599180468360808563945281389781327557747838772170381060813469985856815104'
    expect(jsonNumericKey(numeric('1e100'))).toBe(`i:${actual}`)
    expect(jsonNumericKey(numeric(actual))).toBe(jsonNumericKey(numeric('1e100')))
  })
})

describe('Python wire numeric ordering', () => {
  it.each([
    ['9007199254740992', '9007199254740993'],
    ['9007199254740992.0', '9007199254740993'],
    [`1${'0'.repeat(100)}`, '1e100'],
    ['-9007199254740993', '-9007199254740992.0'],
    ['-1e100', `-1${'0'.repeat(100)}`],
    ['-1000', '-99'], ['-1', '0'], ['0', '1'], ['99', '1000'],
    ['-2', '-1.25'], ['-1.25', '-1'], ['-1', '-0.5'], ['-0.5', '0'],
    ['0', '0.5'], ['0.5', '1'], ['1', '1.25'], ['1.25', '2'],
    ['-5e-324', '0'], ['0', '5e-324'], ['5e-324', '1e-323'],
    ['-1.7976931348623157e308', '1.7976931348623157e308'],
    ['0.1', '0.10000000000000002'], ['-0.10000000000000002', '-0.1'],
    [`-${'9'.repeat(20_000)}`, '-1.7976931348623157e308'],
    ['1.7976931348623157e308', '9'.repeat(20_000)],
  ])('orders %s below %s without rounding and reverses the relation', (left, right) => {
    expect(compareJsonNumeric(numeric(left), numeric(right))).toBe(-1)
    expect(compareJsonNumeric(numeric(right), numeric(left))).toBe(1)
  })

  it.each([true, false, null, undefined, '1', 1n, NaN, Infinity, {}, { raw: '1' }, numeric('1e999')])('rejects non numeric comparison operand %s', (value) => {
    expect(() => compareJsonNumeric(value, 1)).toThrow(TypeError)
    expect(() => compareJsonNumeric(1, value)).toThrow(TypeError)
  })

  it('does not convert original huge integers when comparing their nearby values', () => {
    const left = numeric('9'.repeat(20_000))
    const right = numeric(`1${'0'.repeat(20_000)}`)
    const convert = vi.spyOn(globalThis, 'BigInt').mockImplementation(() => { throw new Error('No integer conversion') })
    try {
      expect(compareJsonNumeric(left, right)).toBe(-1)
      expect(compareJsonNumeric(right, left)).toBe(1)
      expect(convert).not.toHaveBeenCalled()
    } finally { convert.mockRestore() }
  })
})

describe('strict JSON decoding', () => {
  it('round trips nested data and escapes while preserving every numeric token', () => {
    const wire = '{"nested":[null,true,false,"说明😀\\n\\t\\b\\f\\r\\\\\\\"\\/",{"enum":[9007199254740992,9007199254740993,1.0,-0.0,1E+100]}]}'
    const parsed = parseLosslessJson(wire)
    const compact = stringifyLosslessJson(parsed)
    expect(compact).toContain('[9007199254740992,9007199254740993,1.0,-0.0,1E+100]')
    expect(stringifyLosslessJson(parseLosslessJson(compact))).toBe(compact)
    const display = stringifyLosslessJson(parsed, 2)
    expect(display).toContain('\n  "nested": [\n')
    expect(stringifyLosslessJson(parseLosslessJson(display))).toBe(compact)
  })

  it('decodes Unicode escapes and preserves legal lone surrogates for the semantic boundary to validate', () => {
    expect(parseLosslessJson('"\\u4E2D\\ud83d\\ude00"')).toBe('中😀')
    expect(parseLosslessJson('"\\ud800"')).toBe('\ud800')
    expect(stringifyLosslessJson(parseLosslessJson('"\\ud800"'))).toBe('"\\ud800"')
  })

  it('accepts exactly the four JSON whitespace characters', () => {
    expect(parseLosslessJson(' \t\r\n [ 1 , true , null ] \r\n')).toEqual([1, true, null])
  })

  it.each([
    '', ' ', '+1', '01', '-01', '-', '.1', '1.', '1e', '1e+', '1e-', '--1', 'NaN', 'Infinity',
    'undefined', 'True', 'null null', 'truex', '[1,]', '[,1]', '[1 2]', '{"a":1,}', '{a:1}',
    '{"a" 1}', '{"a":}', '{"a":1 "b":2}', '/* hi */ 1', '0x10', '\ufeff1', '\u00a01',
    '"unterminated', '"bad\\x20"', '"bad\\u123"', '"bad\\uZZZZ"', '"bad\nline"', '"bad\0value"',
    '{"a":1,"a":2}', '{"a":1,"\\u0061":2}', '{"😀":1,"\\ud83d\\ude00":2}',
  ])('rejects invalid or ambiguous input %j', (wire) => {
    expect(() => parseLosslessJson(wire)).toThrow(SyntaxError)
    try { parseLosslessJson(wire) } catch (error) { expect((error as Error).message).toBe('Invalid lossless JSON') }
  })

  it('treats __proto__, constructor and toString as safe own data without pollution', () => {
    const value = parseLosslessJson('{"__proto__":{"polluted":true},"constructor":1,"toString":2}') as Record<string, unknown>
    expect(Object.getPrototypeOf(value)).toBeNull()
    expect(Object.hasOwn(value, '__proto__')).toBe(true)
    expect(Object.hasOwn(Object.prototype, 'polluted')).toBe(false)
    expect(stringifyLosslessJson(value)).toBe('{"__proto__":{"polluted":true},"constructor":1,"toString":2}')
  })

  it('allows 64 containers but rejects the next before stack exhaustion', () => {
    const atLimit = '['.repeat(64) + '1' + ']'.repeat(64)
    expect(stringifyLosslessJson(parseLosslessJson(atLimit))).toBe(atLimit)
    expect(() => parseLosslessJson('['.repeat(65) + '1' + ']'.repeat(65))).toThrow(SyntaxError)
  })

  it('does not depend on native JSON.parse, reviver context, rawJSON or eval', () => {
    const native = vi.spyOn(JSON, 'parse').mockImplementation(() => { throw new Error('Native parse must not run') })
    try {
      expect(stringifyLosslessJson(parseLosslessJson('{"key":[9007199254740993,1.0]}'))).toBe('{"key":[9007199254740993,1.0]}')
      expect(native).not.toHaveBeenCalled()
    } finally { native.mockRestore() }
  })

  it('matches native JSON grammar for 512 deterministic valid nested samples', () => {
    for (const wire of grammarSamples()) {
      const decoded = parseLosslessJson(wire)
      // Native number は保真 oracle にせず、文法・文字列・container の比較だけに使う。
      expect(JSON.parse(stringifyLosslessJson(decoded))).toEqual(JSON.parse(wire))
    }
  })

  it('does not accept invalid grammar after deterministic token mutations', () => {
    let rejected = 0
    for (const wire of grammarSamples()) {
      const pivot = Math.floor(wire.length / 2)
      for (const mutant of [wire.slice(0, pivot) + '\\' + wire.slice(pivot), `${wire},`, wire.slice(0, -1)]) {
        let nativeAccepted = true
        try { JSON.parse(mutant) } catch { nativeAccepted = false }
        let decoded: unknown
        let accepted = true
        try { decoded = parseLosslessJson(mutant) } catch (error) {
          expect(error).toBeInstanceOf(SyntaxError)
          accepted = false
          rejected++
        }
        if (accepted) {
          expect(nativeAccepted).toBe(true)
          expect(JSON.parse(stringifyLosslessJson(decoded))).toEqual(JSON.parse(mutant))
        }
      }
    }
    expect(rejected).toBeGreaterThan(1000)
  })

  it('applies the depth limit across mixed object and array nesting', () => {
    const wrap = (depth: number): string => depth === 0 ? 'null' : depth % 2 ? `{"child":${wrap(depth - 1)}}` : `[${wrap(depth - 1)}]`
    expect(stringifyLosslessJson(parseLosslessJson(wrap(64)))).toBe(wrap(64))
    expect(() => parseLosslessJson(wrap(65))).toThrow(SyntaxError)
  })
})

describe('strict lossless serialization', () => {
  it('matches ordinary JSON for JSON data and keeps primitive negative zero', () => {
    const value = { text: '中文\r\n</script>', value: [true, null, 12, 1.25] }
    expect(stringifyLosslessJson(value, 2)).toBe(JSON.stringify(value, null, 2))
    expect(stringifyLosslessJson(-0)).toBe('-0')
  })

  it.each([undefined, () => 1, Symbol('hidden'), 1n, NaN, Infinity, -Infinity, new Date(0), new Map(), new Set(), new Number(1)])('refuses non JSON data %s instead of silently converting it', (value) => {
    expect(() => stringifyLosslessJson(value)).toThrow(TypeError)
    expect(() => stringifyLosslessJson({ value })).toThrow(TypeError)
  })

  it('does not call getters or custom toJSON', () => {
    const getter = vi.fn(() => 1)
    const toJSON = vi.fn(() => 1)
    const accessor = Object.defineProperty({}, 'secret', { get: getter, enumerable: true })
    const getterArray = Object.defineProperty([1], '0', { get: getter, enumerable: true })
    for (const item of [accessor, getterArray, { toJSON }, Object.create({ inherited: true })]) {
      expect(() => stringifyLosslessJson(item)).toThrow(TypeError)
    }
    expect(getter).not.toHaveBeenCalled()
    expect(toJSON).not.toHaveBeenCalled()
  })

  it('rejects hidden fields, symbol keys, sparse arrays and extra array fields', () => {
    const hidden = Object.defineProperty({}, 'private', { value: 1 })
    const symbol = { [Symbol('x')]: 1 }
    const sparse = new Array(1)
    const extra = Object.assign([1], { other: 2 })
    for (const item of [hidden, symbol, sparse, extra, [undefined]]) expect(() => stringifyLosslessJson(item)).toThrow(TypeError)
  })

  it('refuses cycles but allows reused child data', () => {
    const cycle: unknown[] = []
    cycle.push(cycle)
    expect(() => stringifyLosslessJson(cycle)).toThrow(TypeError)
    const child = { value: 1 }
    expect(stringifyLosslessJson([child, child])).toBe('[{"value":1},{"value":1}]')
  })

  it('applies the same depth guard to caller-created structures', () => {
    let value: unknown = 1
    for (let depth = 0; depth < 65; depth++) value = [value]
    expect(() => stringifyLosslessJson(value)).toThrow(TypeError)
  })

  it('bounds indentation but does not truncate data', () => {
    expect(stringifyLosslessJson([1], 100)).toBe('[\n          1\n]')
    expect(stringifyLosslessJson([1], -2)).toBe('[1]')
    expect(() => stringifyLosslessJson([1], NaN)).toThrow(TypeError)
  })
})
