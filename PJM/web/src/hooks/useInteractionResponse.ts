import { useCallback, useLayoutEffect, useRef, useState } from 'react'

import { respondToInteraction, type InteractionAnswerInput, type RespondedInteractionRecord, type UserInteractionDetail } from '../api'
import { createIdempotencyKey } from '../lib/idempotency'
import { canConfirmInteraction, freezeInteractionResponse, interactionAccessFailure, interactionPayload, INTERACTION_REQUEST_POLICY, sameInteractionIdentity,
  type InteractionAccessFailure, type InteractionFailure, type InteractionScope, type PendingInteractionResponse } from '../lib/interactionResponse'
import { useResourceMutation, type SessionEnded } from './useResourceRequest'

/** 親の actor/Session/Project/Run key と card の Interaction key が生存範囲を固定する。 */
export function useInteractionResponse({ scope, csrfToken, interaction, available, writable, accessFailure = null, onResponded, onSessionExpired }: {
  scope: InteractionScope
  csrfToken: string
  interaction: UserInteractionDetail
  available: boolean
  writable: boolean
  accessFailure?: InteractionAccessFailure | null
  onResponded?: (response: RespondedInteractionRecord) => void
  onSessionExpired: SessionEnded
}) {
  const [pending, setPending] = useState<PendingInteractionResponse | null>(null)
  const original = useRef<PendingInteractionResponse | null>(null)
  const [denied, setDenied] = useState<InteractionAccessFailure | null>(null)
  const deniedRef = useRef<InteractionAccessFailure | null>(null)
  const current = useRef({ scope, csrfToken, interaction, available, writable, accessFailure })
  current.current = { scope, csrfToken, interaction, available, writable, accessFailure }
  /** State render を待たずに原要求を守り、同 tick に次の草稿が割り込むのを止める。 */
  const publish = useCallback((value: PendingInteractionResponse | null) => {
    original.current = value
    setPending(value)
  }, [])
  /** 拒否後の再読取や欠落した prop で gate を再開せず、原要求も取消済みにしない。 */
  const denyAccess = useCallback((failure: InteractionAccessFailure): void => {
    deniedRef.current = failure
    setDenied((prior) => prior?.key === failure.key ? prior : failure)
    const value = original.current
    if (value && value.failure?.key !== failure.key) {
      publish({ ...value, phase: value.phase === 'confirmed' ? 'confirmed' : 'rejected', failure })
    }
  }, [publish])
  /** 401 の callback を省略する request hook の仕様に合わせ、失効後も送信中と見せない。 */
  const sessionEnded = useCallback<SessionEnded>(() => {
    if (deniedRef.current?.key === 'sessionExpired') return
    denyAccess({ key: 'sessionExpired' })
    onSessionExpired()
  }, [onSessionExpired, denyAccess])
  const mutation = useResourceMutation(sessionEnded, INTERACTION_REQUEST_POLICY)

  /** 原要求を初回/確認とも同じ transport 経路へ渡し、明示操作以外では再送しない。 */
  function send(value: PendingInteractionResponse): void {
    if (original.current?.phase === 'sending') return
    const request = value.request
    const live = current.current
    if (!live.available || live.accessFailure || deniedRef.current || !live.scope.actorId || !live.csrfToken
      || !sameInteractionIdentity(request.actorId, live.scope.actorId)
      || !sameInteractionIdentity(request.projectId, live.scope.projectId)
      || !sameInteractionIdentity(request.runId, live.scope.runId)
      || !sameInteractionIdentity(request.interactionId, live.interaction.interaction_id)) return
    mutation.acknowledge()
    const accepted = mutation.submit(
      (signal) => respondToInteraction(request.projectId, request.runId, request.interactionId,
        request.version, interactionPayload(request), request.idempotencyKey, live.csrfToken, signal),
      (receipt) => {
        const refusal = deniedRef.current ?? current.current.accessFailure
        if (refusal) {
          publish({ ...value, phase: 'rejected', uncertain: true, failure: refusal })
          return
        }
        publish({ ...value, phase: 'confirmed', failure: null, receipt })
        onResponded?.(receipt)
      },
      (failure: InteractionFailure) => {
        const refusal = deniedRef.current ?? current.current.accessFailure
        publish({ ...value,
          phase: !refusal && (failure.key === 'unknown' || failure.key === 'conflict' || failure.key === 'expired')
            ? failure.key : 'rejected',
          uncertain: value.uncertain || failure.key === 'unknown', failure: refusal ?? failure,
        })
      },
    )
    if (accepted) publish({ ...value, phase: 'sending', failure: null })
  }

  /** 現在の質問と明示草稿からだけ新しい key を生成する。 */
  function start(answer: InteractionAnswerInput): void {
    const live = current.current
    if (original.current || !live.available || !live.writable || live.interaction.status !== 'OPEN'
      || live.interaction.interaction_type === 'EFFECT_APPROVAL') return
    send({ request: freezeInteractionResponse(live.scope, live.interaction, answer, createIdempotencyKey()),
      phase: 'unknown', uncertain: false, failure: null, receipt: null })
  }

  /** 同 key/payload の確認は初回保存にもなり得る。GET 結果や編集草稿で置き換えない。 */
  function confirmOriginal(): void {
    const value = original.current
    if (!value || !canConfirmInteraction(value)) return
    send(value)
  }

  /** 初回の明確な拒否だけ編集へ戻せる。先行 unknown のある要求は消さない。 */
  function edit(): void {
    const value = original.current
    if (value?.phase !== 'rejected' || value.uncertain || value.failure?.key !== 'invalidAnswer') return
    publish(null)
  }

  /** 原 GET の明確な参照拒否は資格 gate を閉じるが、先行 unknown を取消済みにしない。 */
  const rejectAccess = useCallback((failure: InteractionFailure): void => {
    const refusal = interactionAccessFailure(failure)
    if (!refusal) return
    denyAccess(refusal)
    mutation.interrupt()
  }, [denyAccess, mutation.interrupt])

  // 自発 GET の拒否も同じ gate へ通す。通常の loading/500 は原書込を中断しない。
  useLayoutEffect(() => {
    const refusal = accessFailure ?? denied
    if (refusal) rejectAccess(refusal)
    else if (!available) mutation.interrupt()
  }, [accessFailure, denied, available, rejectAccess, mutation.interrupt])

  return { pending, start, confirmOriginal, edit, rejectAccess, onReadSessionExpired: sessionEnded,
    accessFailure: accessFailure ?? denied, busy: mutation.busy }
}
