import { useState } from 'react'

import type { RespondedInteractionRecord, RunDetailRecord, UserInteractionDetail } from '../api'
import type { SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { sameInteractionIdentity, type InteractionScope } from '../lib/interactionResponse'
import { InteractionCard } from './InteractionCard'
import type { RunDetailState } from './RunResultPanel'
import '../styles/interactions.css'

/** 同じ Run の監査 entry は更新で消さず、消えた entry から新しい答復を作らない。 */
function mergeInteractions(previous: UserInteractionDetail[], incoming: UserInteractionDetail[]): UserInteractionDetail[] {
  const merged = new Map(previous.map((item) => [item.interaction_id.toLowerCase(), item]))
  for (const item of incoming) merged.set(item.interaction_id.toLowerCase(), item)
  return [...merged.values()]
}

/** 普通答復の単一所有者。結果/技術折畳み/OPEN→settled は state の寿命を変えない。 */
export function RunInteractions({ scope, state, csrfToken, onResponded, onFacts, onSessionExpired }: {
  scope: InteractionScope
  state: RunDetailState
  csrfToken: string
  onResponded?: (response: RespondedInteractionRecord) => void
  onFacts?: (detail: RunDetailRecord) => void
  onSessionExpired: SessionEnded
}) {
  const messages = useMessages()
  const detail = 'detail' in state ? state.detail ?? null : null
  const valid = detail && sameInteractionIdentity(detail.project_id, scope.projectId)
    && sameInteractionIdentity(detail.run_id, scope.runId) ? detail : null
  const [saved, setSaved] = useState({ source: valid, items: valid?.interactions ?? [] })
  // 同じ component 内で前 props を照合し、次の commit 前に安定した同一 key の一覧へ更新する。
  if (valid && saved.source !== valid) setSaved({ source: valid, items: mergeInteractions(saved.items, valid.interactions) })
  if (!saved.items.length) return null
  // 保持した旧質問を、現在の Run が待機中という証拠にしない。
  const hasOpen = state.status === 'ready'
    && (valid?.status === 'WAITING_FOR_INPUT' || valid?.status === 'WAITING_FOR_APPROVAL')
    && valid.interactions.some((item) => item.status === 'OPEN')
  return <section className="runInteractions resultSection">
    <h3>{hasOpen ? messages.runResult.pendingTitle : messages.interactionResponse.recordsTitle}</h3>
    {hasOpen && <p className="hint">{messages.runResult.pendingHint}</p>}
    {saved.items.map((interaction) => <InteractionCard key={interaction.interaction_id.toLowerCase()}
      scope={scope} interaction={interaction} csrfToken={csrfToken} onResponded={onResponded}
      onFacts={onFacts} onSessionExpired={onSessionExpired}
      accessFailure={state.status === 'error' ? state.accessFailure : undefined}
      available={Boolean(valid?.interactions.some((item) => sameInteractionIdentity(item.interaction_id, interaction.interaction_id)))}
      writable={state.status === 'ready' && valid?.status === 'WAITING_FOR_INPUT'} />)}
  </section>
}
