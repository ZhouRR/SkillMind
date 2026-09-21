import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  adjustInterpretation, confirmInterpretationRequest, interpretSkillSource, loadInterpretationExecution,
  subscribeInterpretEvents, type InterpretationExecutionRecord, type InterpretationLaunchRecord,
  type InterpretEventRecord, type InterpretEventSubscription,
} from '../api'
import { useMessages } from '../i18n'
import { apiErrorMessage } from '../lib/apiFeedback'
import { createIdempotencyKey } from '../lib/idempotency'
import { clearInterpretationReceipt, loadInterpretationReceipt, saveInterpretationReceipt } from '../lib/interpretationReceipt'
import { sameUuid } from '../lib/validation'

/** Model interpret/reinterpret の非同期状態。 */
export type InterpretState =
  | { status: 'idle' }
  | { status: 'unknown'; message: string }
  | { status: 'interpreting'; prompt: string; output: string; attempt: number }
  | { status: 'ready'; execution: InterpretationExecutionRecord }
  | { status: 'error'; message: string }

/** 追加調整（reinterpretation）の非同期状態。 */
export type AdjustState = { status: 'idle' } | { status: 'adjusting' } | { status: 'error'; message: string }

/** 原要求 UUID、SSE observer と保存済み結果の復元を一箇所で所有する。 */
export function useSkillInterpretation(csrfToken: string, onStarted: () => void, onRestore: () => void) {
  const messages = useMessages()
  const [interpretState, setInterpretState] = useState<InterpretState>({ status: 'idle' })
  const [adjustState, setAdjustState] = useState<AdjustState>({ status: 'idle' })
  const [instruction, setInstruction] = useState('')
  const [pendingRequestId, setPendingRequestId] = useState<string | null>(null)
  const interpretController = useRef<AbortController | null>(null)
  const interpretStream = useRef<InterpretEventSubscription | null>(null)
  const pendingInterpretation = useRef<string | null>(null)
  const completedInterpretation = useRef<string | null>(null)

  useEffect(() => {
    try {
      const originalId = loadInterpretationReceipt()
      if (originalId !== null) {
        pendingInterpretation.current = originalId
        setPendingRequestId(originalId)
        onRestore()
        void confirmPendingInterpretation()
      }
    } catch {
      onRestore()
      setInterpretState({ status: 'unknown', message: messages.skills.interpretStorageFailure })
    }
  }, [])

  // 観測の復元とは別に、離頁 commit の時点で遅延 callback の所有権を閉じる。
  useLayoutEffect(() => () => {
    interpretController.current?.abort()
    interpretStream.current?.close()
    completedInterpretation.current = null
  }, [])

  /** 後続 state を idle へ戻し、古い解釈/版本を残さないようにする。 */
  function resetInterpretation(): void {
    // 未決要求は保持し、利用者が新しい原文へ進むときだけ前の確認済み表示を片付ける。
    if (pendingInterpretation.current === null) {
      completedInterpretation.current = null
      try { clearInterpretationReceipt() } catch { /* 保存済み原文の操作を表示履歴で止めない。 */ }
    }
    interpretController.current?.abort()
    interpretStream.current?.close()
    interpretStream.current = null
    setInterpretState(pendingInterpretation.current === null
      ? { status: 'idle' }
      : { status: 'unknown', message: messages.skills.interpretUnknown })
    setAdjustState({ status: 'idle' })
  }

  /** 実行中の observer だけを更新し、旧対象/旧 controller の遅延通知を捨てる。 */
  function currentInterpretation(controller: AbortController): boolean {
    return interpretController.current === controller && !controller.signal.aborted
  }

  /** 原 UUID は送信より先に保持し、重複内容の応答では元の UUID に付け替える。 */
  function rememberInterpretation(requestId: string): void {
    completedInterpretation.current = null
    pendingInterpretation.current = requestId
    setPendingRequestId(requestId)
    saveInterpretationReceipt(requestId)
  }

  /** 通信不明は元 UUID の確認だけを許し、FAILED として再生成ボタンへ流さない。 */
  function interpretationUnknown(message = messages.skills.interpretUnknown): void {
    interpretStream.current?.close()
    interpretStream.current = null
    setAdjustState({ status: 'idle' })
    setInterpretState({ status: 'unknown', message })
  }

  /** 確認済み終態か明示した観測終了でのみ手元の原 UUID を片付ける。 */
  function forgetInterpretation(): void {
    clearInterpretationReceipt()
    completedInterpretation.current = null
    pendingInterpretation.current = null
    setPendingRequestId(null)
  }

  /** 元要求の read で進行を再確認し、書込や nonce 生成は行わない。 */
  async function confirmPendingInterpretation(): Promise<void> {
    const originalId = pendingInterpretation.current
    if (originalId === null) return
    interpretController.current?.abort()
    interpretStream.current?.close()
    const controller = new AbortController()
    interpretController.current = controller
    setInterpretState({ status: 'interpreting', prompt: '', output: '', attempt: 0 })
    try {
      const state = await confirmInterpretationRequest(originalId, controller.signal)
      if (currentInterpretation(controller)) await driveLaunch(state, controller)
    } catch (error: unknown) {
      if (currentInterpretation(controller)) interpretationUnknown(
        `${messages.skills.interpretUnknown} ${apiErrorMessage(error, '', messages)}`,
      )
    }
  }

  /** 元要求から observer を外すだけで、model の取消や失敗は宣言しない。 */
  function dismissInterpretation(): void {
    interpretController.current?.abort()
    interpretStream.current?.close()
    interpretStream.current = null
    try {
      forgetInterpretation()
      setInterpretState({ status: 'idle' })
      setAdjustState({ status: 'idle' })
    } catch {
      interpretationUnknown(messages.skills.interpretStorageFailure)
    }
  }

  /** 持久状態を唯一の終態根拠にし、SSE は prompt/delta の表示だけに使う。 */
  async function driveLaunch(launch: InterpretationLaunchRecord, controller: AbortController): Promise<void> {
    if (!currentInterpretation(controller)) return
    interpretStream.current?.close()
    interpretStream.current = null
    rememberInterpretation(launch.request_id)
    if (launch.status === 'SUCCEEDED' || (launch.status === 'FAILED' && launch.interpretation_id !== null)) {
      if (launch.interpretation_id === null) throw new Error('Missing interpretation result')
      const execution = await loadInterpretationExecution(launch.interpretation_id, controller.signal)
      if (!currentInterpretation(controller)) return
      if (!sameUuid(execution.interpretation_id, launch.interpretation_id)
        || !sameUuid(execution.skill_source_id, launch.skill_source_id)
        || execution.execution_key !== launch.execution_key
        || execution.status !== (launch.status === 'SUCCEEDED' ? 'PREVIEW_READY' : 'FAILED')) {
        throw new Error('Interpretation result did not match')
      }
      // 完了後も元 UUID だけを残し、refresh/他画面から戻った際は GET で結果を復元する。
      // 進行中の要求としては扱わず、調整・下書き作成を妨げない。
      pendingInterpretation.current = null
      completedInterpretation.current = execution.interpretation_id
      setPendingRequestId(null)
      setAdjustState({ status: 'idle' })
      setInterpretState({ status: 'ready', execution })
      return
    }
    if (launch.status === 'FAILED' || launch.status === 'REVOKED') {
      forgetInterpretation()
      setAdjustState({ status: 'idle' })
      setInterpretState({ status: 'error', message: messages.skills.interpretFailedCode(launch.error_code ?? 'unknown') })
      return
    }
    if (launch.status === 'UNKNOWN') {
      interpretationUnknown()
      return
    }
    setAdjustState({ status: 'idle' })
    setInterpretState({ status: 'interpreting', prompt: '', output: '', attempt: 0 })
    interpretStream.current = subscribeInterpretEvents(
      launch.request_id, launch.execution_key,
      (event) => {
        if (currentInterpretation(controller) && pendingInterpretation.current === launch.request_id) {
          handleInterpretEvent(event)
        }
      },
      () => { if (currentInterpretation(controller)) interpretationUnknown() },
    )
  }

  /** 終端通知も元要求を GET して確認し、通知の result ID を直接採用しない。 */
  function handleInterpretEvent(event: InterpretEventRecord): void {
    if (event.event === 'interpret.prompt') {
      const system = typeof event.data.system_prompt === 'string' ? event.data.system_prompt : ''
      const user = typeof event.data.user_message === 'string' ? event.data.user_message : ''
      setInterpretState((current) => current.status === 'interpreting'
        ? { ...current, prompt: `${system}\n\n---\n\n${user}`, output: '', attempt: current.attempt + 1 }
        : current)
    } else if (event.event === 'interpret.delta') {
      const text = typeof event.data.text === 'string' ? event.data.text : ''
      setInterpretState((current) => current.status === 'interpreting'
        ? { ...current, output: current.output + text } : current)
    } else if (event.event === 'interpret.completed' || event.event === 'interpret.failed') {
      void confirmPendingInterpretation()
    } else if (event.event === 'interpret.unknown' || event.event === 'interpret.disconnected') {
      interpretationUnknown()
    }
  }

  /** 新しい明示要求だけに UUID を生成し、保留中の要求を上書きしない。 */
  async function handleInterpret(skillSourceId: string, forceRegenerate = false): Promise<void> {
    if (pendingInterpretation.current !== null || interpretState.status === 'unknown') return
    interpretController.current?.abort()
    const controller = new AbortController()
    interpretController.current = controller
    const requestId = createIdempotencyKey()
    try { rememberInterpretation(requestId) } catch {
      interpretationUnknown(messages.skills.interpretStorageFailure)
      return
    }
    setInterpretState({ status: 'interpreting', prompt: '', output: '', attempt: 0 })
    setAdjustState({ status: 'idle' })
    onStarted()
    try {
      const launch = await interpretSkillSource(skillSourceId, requestId, csrfToken, controller.signal, forceRegenerate)
      if (currentInterpretation(controller)) await driveLaunch(launch, controller)
    } catch (error: unknown) {
      if (currentInterpretation(controller)) interpretationUnknown(
        `${messages.skills.interpretUnknown} ${apiErrorMessage(error, '', messages)}`,
      )
    }
  }

  /** 調整も先に元 UUID を保持し、不明な応答を自動再送しない。 */
  async function handleAdjust(interpretationId: string): Promise<void> {
    const text = instruction.trim()
    if (!text || pendingInterpretation.current !== null || interpretState.status === 'unknown') return
    interpretController.current?.abort()
    const controller = new AbortController()
    interpretController.current = controller
    const requestId = createIdempotencyKey()
    try { rememberInterpretation(requestId) } catch {
      interpretationUnknown(messages.skills.interpretStorageFailure)
      return
    }
    setAdjustState({ status: 'adjusting' })
    onStarted()
    try {
      const launch = await adjustInterpretation(interpretationId, text, requestId, csrfToken, controller.signal)
      if (!currentInterpretation(controller)) return
      setInstruction('')
      await driveLaunch(launch, controller)
    } catch (error: unknown) {
      if (currentInterpretation(controller)) interpretationUnknown(
        `${messages.skills.interpretUnknown} ${apiErrorMessage(error, '', messages)}`,
      )
    }
  }

  /** 下書きになった同じ確認済み解釈だけを片付け、新しい調整要求の receipt は守る。 */
  function acknowledgeDraft(interpretationId: string): void {
    if (pendingInterpretation.current !== null || completedInterpretation.current === null
      || !sameUuid(completedInterpretation.current, interpretationId)) return
    try { clearInterpretationReceipt() } catch { /* 保存成功を表示履歴の障害で覆さない。 */ }
    completedInterpretation.current = null
  }

  return { interpretState, adjustState, instruction, setInstruction, pendingRequestId,
    resetInterpretation, confirmPendingInterpretation, dismissInterpretation,
    handleInterpret, handleAdjust, acknowledgeDraft }
}
