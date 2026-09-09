import { useCallback, useLayoutEffect, useRef, useState } from 'react'

import { ApiProblemError, changeScheduleStatus, loadSchedule, updateSchedule,
  type ScheduleRecord, type ScheduleStatus, type UpdateScheduleInput } from '../api'
import { classifyScheduleReadFailure } from '../lib/scheduleManager'
import { sameUuid } from '../lib/validation'
import { useResourceMutation, useResourceQuery, type ResourceRequestPolicy, type SessionEnded } from './useResourceRequest'

/** 既存 Schedule への編集/状態変更は同じ版の比較と未知結果の門禁を使う。 */
export type ScheduleChange =
  | { kind: 'edit'; input: Omit<UpdateScheduleInput, 'expected_row_version'> }
  | { kind: 'status'; status: ScheduleStatus }

/** 送信時の原値と原 payload。現在一覧や後続草稿では置き換えない。 */
export interface ScheduleIntent {
  original: ScheduleRecord; change: ScheduleChange
  payload: UpdateScheduleInput | { status: ScheduleStatus; expected_row_version: number }
}
/** message は catalog のキーだけに限定し、Problem 本文をそのまま表示しない。 */
export interface ScheduleRevisionFailure {
  key: 'conflict' | 'unknown' | 'rejected' | 'denied' | 'readFailed'
  sessionExpired?: boolean
}
/** 読取と書込の共通 transport は維持し、業務上の拒否/未知だけを分類する。 */
const POLICY: ResourceRequestPolicy<ScheduleRevisionFailure> = {
  classify: (error, write) => {
    const read = classifyScheduleReadFailure(error)
    if (read.key !== 'loadFailed') return { key: 'denied', sessionExpired: read.key === 'sessionExpired' }
    if (!write) return { key: 'readFailed' }
    if (error instanceof ApiProblemError && error.status === 409) return { key: 'conflict' }
    if (error instanceof ApiProblemError && [400, 422].includes(error.status)) return { key: 'rejected' }
    return { key: 'unknown' }
  },
  readTimeout: { key: 'readFailed' }, writeTimeout: { key: 'unknown' },
  blocks: ({ key }) => key !== 'rejected' && key !== 'readFailed',
}
/** 会話全体の書換えは親の責任。ここでは元 editor の門禁だけを閉じる。 */
function retainSession(): void {}

/** 版の変化は owner の変化ではない。呼出側は actor/Project/Schedule/CSRF でのみ mount を分離する。 */
export function useScheduleRevision({ schedule, projectId, csrfToken, isCurrent, disabled = false, syncLatestWhenIdle = false, onSessionEnded, isWriteAllowed }: {
  schedule: ScheduleRecord | null; projectId: string; csrfToken: string
  isCurrent: () => boolean; disabled?: boolean; syncLatestWhenIdle?: boolean
  onSessionEnded?: SessionEnded
  isWriteAllowed?: () => boolean
}) {
  const [base, setBase] = useState(() => schedule ? structuredClone(schedule) : null)
  const baseRef = useRef(base)
  const [intent, setIntent] = useState<ScheduleIntent | null>(null)
  const intentRef = useRef<ScheduleIntent | null>(null)
  const [phase, setPhase] = useState<'ready' | 'sending' | 'conflict' | 'unknown' | 'denied'>('ready')
  const phaseRef = useRef(phase)
  const [previousUnknown, setPreviousUnknown] = useState<ScheduleIntent | null>(null)
  const [accessDenied, setAccessDenied] = useState(false)
  const deniedRef = useRef(false)
  const disabledRef = useRef(disabled)
  const writeAllowedRef = useRef(isWriteAllowed)
  writeAllowedRef.current = isWriteAllowed
  const endedRef = useRef(onSessionEnded)
  const endedSent = useRef(false)
  endedRef.current = onSessionEnded
  disabledRef.current = disabled
  // 状態ボタンだけは未操作時の認可済み現在値へ追随する。編集と未決要求の基準版は替えない。
  const currentBase = syncLatestWhenIdle && phase === 'ready' && !intent && !accessDenied
    && schedule && base && schedule.row_version >= base.row_version ? schedule : base
  baseRef.current = currentBase
  const mutation = useResourceMutation(retainSession, POLICY)
  const [ticket, setTicket] = useState<{ intent: ScheduleIntent } | null>(null)
  const ticketRef = useRef(ticket)
  const factsRef = useRef<ScheduleRecord | null>(null)

  /** 旧成功を失効の後から反映させない。HTTP catch 内でも同期門禁を先に閉じる。 */
  const denyAccess = useCallback((sessionExpired = false) => {
    if (!isCurrent()) return
    deniedRef.current = true
    factsRef.current = null
    setAccessDenied(true)
    mutation.interrupt()
    phaseRef.current = 'denied'
    setPhase('denied')
    if (sessionExpired && !endedSent.current) { endedSent.current = true; endedRef.current?.() }
  }, [isCurrent, mutation.interrupt])
  useLayoutEffect(() => { if (disabled) mutation.interrupt() }, [disabled, mutation.interrupt])
  const loader = useCallback(async (signal: AbortSignal): Promise<ScheduleRecord> => {
    if (!ticket || ticketRef.current !== ticket || !isCurrent() || deniedRef.current) {
      throw new DOMException('Obsolete schedule review', 'AbortError')
    }
    try { return await loadSchedule(projectId, ticket.intent.original.schedule_id, signal) }
    catch (error: unknown) {
      if (ticketRef.current === ticket && !signal.aborted && isCurrent()
        && classifyScheduleReadFailure(error).key !== 'loadFailed') {
        denyAccess(classifyScheduleReadFailure(error).key === 'sessionExpired')
      }
      throw error
    }
  }, [ticket, projectId, isCurrent, denyAccess])
  const query = useResourceQuery(projectId + ':' + (base?.schedule_id ?? ''), loader,
    retainSession, POLICY, !!ticket && !accessDenied)
  const facts = ticketRef.current === ticket && ticket && !query.pending && !query.failure
    && !accessDenied ? query.data : null
  factsRef.current = facts

  /** 同 tick の二重送信も原 payload を凍結する前に拒否し、後から別版へすり替えない。 */
  function submit(change: ScheduleChange, onSaved: (record: ScheduleRecord) => void,
    onFailure?: (failure: ScheduleRevisionFailure) => void): boolean {
    const original = baseRef.current
    if (!original || !isCurrent() || writeAllowedRef.current?.() === false || disabledRef.current || deniedRef.current || phaseRef.current !== 'ready'
      || original.status === 'ARCHIVED' || change.kind === 'edit' && original.status === 'COMPLETED') return false
    const frozen = structuredClone(change)
    const payload = frozen.kind === 'edit' ? { ...frozen.input, expected_row_version: original.row_version }
      : { status: frozen.status, expected_row_version: original.row_version }
    const next: ScheduleIntent = { original: structuredClone(original), change: frozen, payload }
    const accepted = mutation.submit(async (signal) => {
      if (!isCurrent() || writeAllowedRef.current?.() === false || disabledRef.current || deniedRef.current) throw new DOMException('Obsolete schedule write', 'AbortError')
      return frozen.kind === 'edit'
        ? updateSchedule(projectId, original.schedule_id, { ...frozen.input, expected_row_version: original.row_version }, csrfToken, signal)
        : changeScheduleStatus(projectId, original.schedule_id, frozen.status, original.row_version, csrfToken, signal)
    }, (record) => {
      if (!isCurrent()) return
      // 親の拒否 ref は disabled の再描画より先に閉じる。旧成功で intent を消さない。
      if (disabledRef.current || deniedRef.current || writeAllowedRef.current?.() === false) {
        const failure: ScheduleRevisionFailure = { key: deniedRef.current ? 'denied' : 'unknown' }
        phaseRef.current = deniedRef.current ? 'denied' : 'unknown'
        setPhase(phaseRef.current)
        onFailure?.(failure)
        return
      }
      baseRef.current = record
      setBase(record)
      phaseRef.current = 'ready'
      setPhase('ready')
      intentRef.current = null
      setIntent(null)
      onSaved(record)
    }, (failure) => {
      if (!isCurrent()) return
      if (failure.key === 'denied') denyAccess(failure.sessionExpired)
      const nextPhase = deniedRef.current ? 'denied' : failure.key === 'rejected' ? 'ready'
        : failure.key === 'conflict' ? 'conflict' : 'unknown'
      phaseRef.current = nextPhase
      setPhase(nextPhase)
      // 確定拒否は未決の状態要求ではない。失敗表示は残し、次の版は親の精確読取から得る。
      if (nextPhase === 'ready' && frozen.kind === 'status') {
        intentRef.current = null
        setIntent(null)
      }
      onFailure?.(failure)
    })
    if (accepted) {
      setBase(structuredClone(original))
      intentRef.current = next
      setIntent(next)
      phaseRef.current = 'sending'
      setPhase('sending')
      factsRef.current = null
      ticketRef.current = null
      setTicket(null)
    }
    return accepted
  }
  /** 明示読取は旧 facts を同期破棄する。同 tick の read→adopt は前回値を使えない。 */
  function reconcile(): void {
    const original = intentRef.current
    if (!original || !isCurrent() || deniedRef.current
      || !['unknown', 'conflict'].includes(phaseRef.current)) return
    factsRef.current = null
    const next = { intent: original }
    ticketRef.current = next
    setTicket(next)
    query.refresh()
  }
  /** GET は元 write の成功証明ではない。新しい基準版だけ採用し、原草稿の再送はしない。 */
  function adopt(): boolean {
    const current = factsRef.current
    const original = intentRef.current
    if (!current || !original || !isCurrent() || writeAllowedRef.current?.() === false || disabledRef.current || deniedRef.current
      || !['unknown', 'conflict'].includes(phaseRef.current)
      || !sameUuid(current.skill_version_id, original.original.skill_version_id)
      || current.task_key !== original.original.task_key) return false
    if (phaseRef.current === 'unknown') setPreviousUnknown(original)
    baseRef.current = structuredClone(current)
    setBase(baseRef.current)
    factsRef.current = null
    ticketRef.current = null
    setTicket(null)
    intentRef.current = null
    setIntent(null)
    mutation.acknowledge()
    phaseRef.current = 'ready'
    setPhase('ready')
    return true
  }
  const canAdopt = !!facts && !!intent && !disabled && !accessDenied && writeAllowedRef.current?.() !== false
    && sameUuid(facts.skill_version_id, intent.original.skill_version_id) && facts.task_key === intent.original.task_key
  return { base: currentBase, intent, phase, previousUnknown, accessDenied, facts, canAdopt,
    pending: phase !== 'ready', locked: disabled || accessDenied || phase !== 'ready' || currentBase?.status === 'ARCHIVED',
    failure: accessDenied ? { key: 'denied' as const }
      : mutation.failure ?? (phase === 'unknown' ? POLICY.writeTimeout : null),
    readPending: !!ticket && query.pending && !accessDenied, readFailure: query.failure,
    submit, reconcile, adopt, denyAccess }
}

/** Review component と form が同じ門禁を参照する。 */
export type ScheduleRevision = ReturnType<typeof useScheduleRevision>
