import type { ScheduleDefinitionInput, ScheduleKind, ScheduleRecord } from '../api'
import { isApiTimestamp } from './validation'

/** 分単位の入力を一つの絶対時刻へ結ぶ候補。重複時刻では利用者が明示選択する。 */
export interface LocalScheduleInstant { instant: string; offset: string }

/** 時間だけの草稿。名称や task 入力は preview の版に混ぜない。 */
export interface ScheduleTimeDraft {
  kind: ScheduleKind
  timezone: string
  cronExpression: string
  runAt: string
  runAtChoice: string
  endAt: string
  endAtChoice: string
  maxRuns: string
}

/** browser の入力時区を一度固定する。規則の時区変更とは別の事実。 */
export function browserScheduleTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

/** Intl の認識しない時区を UTC 等へ暗黙に置換しない。 */
export function isScheduleTimezone(value: string): boolean {
  try { new Intl.DateTimeFormat('en', { timeZone: value }).format(0); return value.length > 0 }
  catch { return false }
}

/** 時区付き暦の比較には固定の数字/暦/24 時間表記を使い、UI 言語へ依存させない。 */
function clockFormatter(timezone: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: timezone, calendar: 'iso8601', numberingSystem: 'latn',
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    second: '2-digit', hourCycle: 'h23',
  })
}

/** Date の local parse を使わず、指定した時区の暦成分だけを取り出す。 */
function localClock(formatter: Intl.DateTimeFormat, instant: number): string {
  const parts = Object.fromEntries(formatter.formatToParts(instant).map(({ type, value }) => [type, value]))
  return `${parts.year!.padStart(4, '0')}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`
}

/** 名前だけの timezone と異なり、選択した時刻に適用される具体的 offset を表示する。 */
export function formatScheduleOffset(minutes: number): string {
  const absolute = Math.abs(minutes)
  return `UTC${minutes < 0 ? '-' : '+'}${String(Math.floor(absolute / 60)).padStart(2, '0')}:${String(absolute % 60).padStart(2, '0')}`
}

/** 全ての分単位 offset を有界に調べ、DST gap/日付補正を拒否し fold の全候補を返す。
 * 近傍の offset サンプリングでは短い遷移を見落とし得るため、Date の暗黙補正に頼らない。 */
export function localScheduleInstants(value: string, timezone: string): LocalScheduleInstant[] {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)
    || Number(value.slice(0, 4)) < 1 || !isApiTimestamp(`${value}:00Z`) || !isScheduleTimezone(timezone)) return []
  const wall = Date.parse(`${value}:00Z`)
  const formatter = clockFormatter(timezone)
  const candidates: LocalScheduleInstant[] = []
  for (let offset = -24 * 60; offset <= 24 * 60; offset += 1) {
    const stamp = wall - offset * 60_000
    if (localClock(formatter, stamp) !== `${value}:00`) continue
    const instant = new Date(stamp).toISOString()
    if (isApiTimestamp(instant) && Number(instant.slice(0, 4)) >= 1) {
      candidates.push({ instant, offset: formatScheduleOffset(offset) })
    }
  }
  return candidates.sort((left, right) => Date.parse(left.instant) - Date.parse(right.instant))
}

/** 一候補だけなら確定し、重複時刻の最初の候補を勝手に選ばない。 */
export function selectedScheduleInstant(candidates: LocalScheduleInstant[], choice: string): string | null {
  if (candidates.length === 1) return candidates[0]!.instant
  return candidates.some((candidate) => candidate.instant === choice) ? choice : null
}

/** Preview/一覧/ONCE 摘要は同じ規則時区と実 offset で表示する。 */
export function formatScheduleTimestamp(value: string, timezone: string): string {
  if (!isApiTimestamp(value) || !isScheduleTimezone(timezone)) return '—'
  const stamp = Date.parse(value)
  const clock = localClock(clockFormatter(timezone), stamp)
  const minutes = (Date.parse(`${clock}Z`) - Math.floor(stamp / 1000) * 1000) / 60_000
  if (!Number.isInteger(minutes)) return '—'
  return `${clock.slice(0, 16).replace('T', ' ')} ${formatScheduleOffset(minutes)} · ${timezone}`
}

/** 入力時区で解決済みの絶対時刻だけを既存 API definition に入れる。 */
export function scheduleDefinition(
  draft: ScheduleTimeDraft, runCandidates: LocalScheduleInstant[], endCandidates: LocalScheduleInstant[],
): ScheduleDefinitionInput | null {
  const timezone = draft.timezone.trim()
  const runAt = selectedScheduleInstant(runCandidates, draft.runAtChoice)
  const endAt = selectedScheduleInstant(endCandidates, draft.endAtChoice)
  const maxRuns = draft.maxRuns === '' ? null : Number(draft.maxRuns)
  if (!isScheduleTimezone(timezone) || (draft.kind === 'ONCE' && !runAt)
    || (draft.endAt !== '' && !endAt)
    || (draft.kind === 'CRON' && !draft.cronExpression.trim())
    || (maxRuns !== null && (!Number.isSafeInteger(maxRuns) || maxRuns < 1 || maxRuns > 100_000))) return null
  return {
    kind: draft.kind, timezone, cron_expression: draft.kind === 'CRON' ? draft.cronExpression.trim() : null,
    run_at: draft.kind === 'ONCE' ? runAt : null, end_at: draft.endAt ? endAt : null, max_runs: maxRuns,
  }
}

/** 保存済み絶対時刻を browser の暦へ投影し、fold では元 instant の offset を選択済みにする。 */
export function scheduleTimeDraft(schedule: ScheduleRecord, timezone: string): ScheduleTimeDraft {
  const local = (value: string | null): string => value && isApiTimestamp(value)
    ? localClock(clockFormatter(timezone), Date.parse(value)).slice(0, 16) : ''
  const choice = (value: string | null): string => value && isApiTimestamp(value)
    ? new Date(Math.floor(Date.parse(value) / 60_000) * 60_000).toISOString() : ''
  return {
    kind: schedule.kind, timezone: schedule.timezone, cronExpression: schedule.cron_expression ?? '',
    runAt: local(schedule.run_at), runAtChoice: choice(schedule.run_at),
    endAt: local(schedule.end_at), endAtChoice: choice(schedule.end_at),
    maxRuns: schedule.max_runs === null ? '' : String(schedule.max_runs),
  }
}

/** 未編集の項目は原値を保持する。end_at の秒/小数を local 分入力で黙って切り捨てない。 */
export function preservedScheduleDefinition(
  draft: ScheduleTimeDraft, initial: ScheduleTimeDraft, original: ScheduleRecord,
  resolved: ScheduleDefinitionInput | null,
): ScheduleDefinitionInput | null {
  if (!resolved) return null
  return {
    ...resolved,
    timezone: draft.timezone === initial.timezone ? original.timezone : resolved.timezone,
    cron_expression: draft.kind === 'CRON' && draft.cronExpression === initial.cronExpression
      ? original.cron_expression : resolved.cron_expression,
    run_at: draft.kind === 'ONCE' && draft.runAt === initial.runAt && draft.runAtChoice === initial.runAtChoice
      ? original.run_at : resolved.run_at,
    end_at: draft.endAt === initial.endAt && draft.endAtChoice === initial.endAtChoice
      ? original.end_at : resolved.end_at,
  }
}
