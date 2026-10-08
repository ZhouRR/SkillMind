import { createContext, useContext, useRef, type ReactNode } from 'react'
import type { PendingProposalDecision, ProposalDecisionScope } from '../lib/proposalDecision'

/** actor/Session/Project/Run owner 内でカードの移動や再読込より長く原要求を保持する。 */
export type ProposalDecisionMemory = Map<string, PendingProposalDecision>
const ProposalDecisionContext = createContext<ProposalDecisionMemory | null>(null)

/** storage が使えない場合でも pending→監査への移動で原要求を失わない。 */
export function ProposalDecisionOwner({ children }: { children: ReactNode }) {
  const memory = useRef<ProposalDecisionMemory>(new Map())
  return <ProposalDecisionContext.Provider value={memory.current}>{children}</ProposalDecisionContext.Provider>
}

/** この Context は外側の認証 owner の key と一緒に破棄される。 */
export function useProposalDecisionMemory(): ProposalDecisionMemory | null { return useContext(ProposalDecisionContext) }

/** UUID の表記差を揃え、別の提案・利用者の原要求を混ぜない。 */
export function proposalMemoryKey(scope: ProposalDecisionScope): string {
  return JSON.stringify([scope.actorId, scope.projectId, scope.runId, scope.proposalId].map((id) => id.toLowerCase()))
}
