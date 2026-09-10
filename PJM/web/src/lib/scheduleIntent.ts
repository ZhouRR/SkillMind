import type {
  CreateScheduleInput, ScheduleDefinitionInput, ScheduleRecord, UpdateScheduleInput,
} from '../api/schedules'
import { apiTimestampMicroseconds, isApiTimestamp, isUuid } from './validation'
import { sameJsonValue as sameJson } from './jsonValue'

type ScheduleMutation = CreateScheduleInput | UpdateScheduleInput
type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }

// Python str.strip/split と同じ集合を使い、JS trim が除く FEFF は保存する。
const PYTHON_SPACE = '[\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]'
const EDGE_SPACE = new RegExp(`^${PYTHON_SPACE}+|${PYTHON_SPACE}+$`, 'g')
const INNER_SPACE = new RegExp(`${PYTHON_SPACE}+`, 'g')
const MINUTE_MICROSECONDS = 60_000_000n

/** 送信前に独立した JSON を凍結し、await 中の草稿変更を成功判定に混ぜない。 */
export function captureScheduleIntent<T extends ScheduleMutation>(input: T, operation: 'create' | 'update'): T {
  const frozen = freezeJson(input) as unknown as T
  const required = operation === 'create'
    ? ['name', 'definition', 'skill_version_id', 'task_key']
    : ['name', 'definition', 'expected_row_version']
  requireShape(frozen, required, ['input', 'sources'])
  if (!boundedString(frozen.name, 200) || !pythonStrip(frozen.name)) invalid()
  validateDefinition(frozen.definition)
  if (operation === 'create') {
    const create = frozen as CreateScheduleInput
    if (!isUuid(create.skill_version_id) || !boundedString(create.task_key, 200)) invalid()
  }
  if (Object.hasOwn(frozen, 'input')) requireObject(frozen.input)
  if (Object.hasOwn(frozen, 'sources')) {
    requireObject(frozen.sources)
    if (Object.keys(frozen.sources!).length > 50 || !Object.values(frozen.sources!).every((item) => typeof item === 'string')) invalid()
  }
  return frozen
}

/** Preview も NaN 等を null に変えず、原 definition を一度だけ送信する。 */
export function captureScheduleDefinition(definition: ScheduleDefinitionInput): ScheduleDefinitionInput {
  const frozen = freezeJson(definition) as unknown as ScheduleDefinitionInput
  validateDefinition(frozen)
  return frozen
}

/** 保存結果だけを照合し、last_* や現在の発火候補から成功を推測しない。 */
export function matchesScheduleIntent(record: ScheduleRecord, intent: ScheduleMutation): boolean {
  const definition = intent.definition
  const expectedCron = definition.kind === 'CRON'
    ? pythonStrip(definition.cron_expression!).replace(INNER_SPACE, ' ')
    : null
  return record.name === pythonStrip(intent.name)
    && record.kind === definition.kind && record.timezone === definition.timezone
    && record.cron_expression === expectedCron
    && sameInstant(record.run_at, definition.run_at ?? null, definition.kind === 'ONCE')
    && sameInstant(record.end_at, definition.end_at ?? null)
    && record.max_runs === (definition.max_runs ?? null)
    && sameJson(record.input, intent.input ?? {}) && sameJson(record.sources, intent.sources ?? {})
}

/** 時刻の許容形状だけを守り、cron 求値・IANA DB・将来時刻判定は server に任せる。 */
function validateDefinition(definition: ScheduleDefinitionInput): void {
  requireShape(definition, ['kind', 'timezone'], ['cron_expression', 'run_at', 'end_at', 'max_runs'])
  if (!['ONCE', 'CRON'].includes(definition.kind) || !boundedString(definition.timezone, 64)) invalid()
  const cron = definition.cron_expression
  if (cron !== undefined && cron !== null && (typeof cron !== 'string' || [...cron].length > 128)) invalid()
  if (definition.kind === 'CRON' && (!cron || definition.run_at != null)) invalid()
  if (definition.kind === 'ONCE' && (definition.run_at == null || Boolean(cron))) invalid()
  for (const value of [definition.run_at, definition.end_at]) {
    if (value != null && (!isApiTimestamp(value) || Number(value.slice(0, 4)) < 1)) invalid()
  }
  const maximum = definition.max_runs
  if (maximum != null && (typeof maximum !== 'number' || !Number.isSafeInteger(maximum) || maximum < 1 || maximum > 100_000)) invalid()
}

/** JSON.stringify が黙って捨てる値、toJSON 変換、循環を HTTP 前に拒否する。 */
function freezeJson(value: unknown, ancestors = new Set<object>()): JsonValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'object' || value === null || ancestors.has(value)) return invalid()
  const array = Array.isArray(value)
  const prototype = Object.getPrototypeOf(value)
  if (array ? prototype !== Array.prototype : ![Object.prototype, null].includes(prototype)) return invalid()
  ancestors.add(value)
  try {
    const result: JsonValue[] | { [key: string]: JsonValue } = array ? [] : Object.create(null) as { [key: string]: JsonValue }
    const keys = Reflect.ownKeys(value)
    if (array && keys.length !== value.length + 1) invalid()
    for (const key of keys) {
      if (array && key === 'length') continue
      const descriptor = Object.getOwnPropertyDescriptor(value, key)!
      if (typeof key !== 'string' || !descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) invalid()
      if (array && (!/^(0|[1-9]\d*)$/.test(key) || Number(key) >= value.length)) invalid()
      const item = freezeJson(descriptor.value, ancestors)
      if (Array.isArray(result)) result[Number(key)] = item
      else result[key] = item
    }
    Object.freeze(result)
    return result
  } finally {
    ancestors.delete(value)
  }
}

/** Offset は同じ instant として比較し、end_at の microsecond を millisecond へ丸めない。 */
function sameInstant(actual: string | null, expected: string | null, floorMinute = false): boolean {
  if (actual === null || expected === null) return actual === expected
  let instant = apiTimestampMicroseconds(expected)
  if (floorMinute) instant -= ((instant % MINUTE_MICROSECONDS) + MINUTE_MICROSECONDS) % MINUTE_MICROSECONDS
  return apiTimestampMicroseconds(actual) === instant
}

/** Optional field の欠落だけを許し、余分な要求を送信してから発見しない。 */
function requireShape(value: unknown, required: string[], optional: string[]): void {
  requireObject(value)
  if (!required.every((key) => Object.hasOwn(value, key)) || Object.keys(value).some((key) => !required.includes(key) && !optional.includes(key))) invalid()
}

/** 入力 JSON の root と sources は array/null で代用しない。 */
function requireObject(value: unknown): asserts value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalid()
}

/** Pydantic の文字数と同じく UTF-16 code unit ではなく code point を数える。 */
function boundedString(value: unknown, maximum: number): value is string {
  return typeof value === 'string' && [...value].length >= 1 && [...value].length <= maximum
}

/** 保存名の規則だけを合わせ、送信した元の文字列自体は書き換えない。 */
function pythonStrip(value: string): string {
  return value.replace(EDGE_SPACE, '')
}

/** 原入力を error に含めず、局所の不正値として送信を止める。 */
function invalid(): never {
  throw new Error('Schedule request is invalid')
}
