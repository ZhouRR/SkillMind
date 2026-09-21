import type { AgentSessionDetail, EvidenceDetail } from '../api'

/** 一回の扇出の投影。証拠 metadata が唯一の出所で、画面側では再計算しない。 */
export interface SubagentDispatch {
  evidenceRef: string
  objective: string
  branches: Array<{ key: string; outcome: string; session: string }>
  turnsPerBranch: number | null
  outputBytesPerBranch: number | null
}

/** `subagent-dispatch` 証拠から扇出の投影を取り出す。 */
export function collectSubagentDispatches(evidence: readonly EvidenceDetail[]): SubagentDispatch[] {
  return evidence
    .filter((item) => item.evidence_type === 'subagent-dispatch')
    .map((item) => {
      const meta = item.metadata
      const rawBranches = Array.isArray(meta.branches) ? meta.branches : []
      return {
        evidenceRef: item.evidence_ref,
        objective: typeof meta.objective === 'string' ? meta.objective : '',
        branches: rawBranches.flatMap((branch) => {
          if (typeof branch !== 'object' || branch === null) return []
          const record = branch as Record<string, unknown>
          return [{
            key: String(record.key ?? ''),
            outcome: String(record.outcome ?? ''),
            session: String(record.session ?? ''),
          }]
        }),
        turnsPerBranch: typeof meta.turns_per_branch === 'number' ? meta.turns_per_branch : null,
        outputBytesPerBranch:
          typeof meta.output_bytes_per_branch === 'number' ? meta.output_bytes_per_branch : null,
      }
    })
}

/** 扇出で「実際には調べられていない面」があるかを判定する。 */
export function hasIncompleteBranch(dispatches: readonly SubagentDispatch[]): boolean {
  return dispatches.some((item) => item.branches.some((branch) => branch.outcome !== 'COMPLETED'))
}

/** 一つの主分析と、その下で併走した扇出の子分析。 */
export interface SessionLineage {
  primary: AgentSessionDetail
  branches: AgentSessionDetail[]
}

/** Segment 内の Session を主分析ごとにまとめ、子分析をその下へ畳む。
 *
 *  平坦に並べると「順に 5 回会話した」と読めてしまうが、実際は主分析 1 本と併走した子分析 4 本
 *  かもしれない。件数の意味が変わるので、主分析だけを数え、子はぶら下げる。
 *  親が見つからない子は**捨てずに**独立した行として残す——監査記録から黙って消える方が悪い。 */
export function groupSessionsByLineage(
  sessions: readonly AgentSessionDetail[],
): SessionLineage[] {
  const lineages: SessionLineage[] = []
  const byId = new Map<string, SessionLineage>()
  for (const session of sessions) {
    if (session.session_kind !== 'SUBAGENT') {
      const lineage: SessionLineage = { primary: session, branches: [] }
      lineages.push(lineage)
      byId.set(session.agent_session_id, lineage)
    }
  }
  for (const session of sessions) {
    if (session.session_kind !== 'SUBAGENT') continue
    const parent = session.parent_session_id === null ? undefined : byId.get(session.parent_session_id)
    if (parent === undefined) {
      lineages.push({ primary: session, branches: [] })
      continue
    }
    parent.branches.push(session)
  }
  return lineages
}
