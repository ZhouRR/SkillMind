import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiProblemError, loadProject, loadProjects, type ProjectRecord } from '../api'
import { useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { PROJECT_REQUEST_POLICY } from '../lib/projectFeedback'
import type { ProjectIntent } from '../lib/projectManagement'
import { ProjectComparison, ProjectDraftFacts, ProjectFacts, ProjectResponseNotice } from './ProjectManagementElements'

/** 独立した読取の事実。不可読や key 未発見も原 write の rollback 証明ではない。 */
interface ProjectReviewFacts { projects: ProjectRecord[]; unavailable: boolean }

/** 衝突/未知の元 ID、または新規作成の元 key だけを明示的に再読取する。 */
export function ProjectManagementReview({ intent, unknown, onSessionEnded, onAdopt, onAcknowledge, onCancel }: {
  intent: ProjectIntent
  unknown: boolean
  onSessionEnded: SessionEnded
  onAdopt: (project: ProjectRecord) => void
  onAcknowledge: () => void
  onCancel: () => void
}) {
  const messages = useMessages()
  const [started, setStarted] = useState(false)
  const [checked, setChecked] = useState(false)
  const checkedRef = useRef(false)
  const factsHeading = useRef<HTMLHeadingElement>(null)
  const loader = useCallback(async (signal: AbortSignal): Promise<ProjectReviewFacts> => {
    if (intent.action === 'create') {
      const projects = await loadProjects(true, signal)
      return { projects: projects.filter((project) => project.key === intent.draft.key), unavailable: false }
    }
    if (!intent.base) throw new Error('Project review requires its original identity')
    try { return { projects: [await loadProject(intent.base.project_id, signal)], unavailable: false } }
    catch (error) {
      if (error instanceof ApiProblemError && error.status === 404 && error.code === 'project_not_found') return { projects: [], unavailable: true }
      throw error
    }
  }, [intent])
  const query = useResourceQuery(JSON.stringify([intent.action, intent.base?.project_id, intent.draft.key]), loader, onSessionEnded, PROJECT_REQUEST_POLICY, started)
  const ready = started && !query.pending && !query.failure && query.completed >= 0 && !!query.data
  const currentFacts = useRef(false)
  currentFacts.current = ready
  useEffect(() => { if (ready) factsHeading.current?.focus() }, [ready])

  /** 読み直した瞬間に旧 checkbox の同 tick acknowledgment も無効にする。 */
  function read(): void {
    currentFacts.current = false
    checkedRef.current = false
    setChecked(false)
    setStarted(true)
    query.refresh()
  }
  /** 現在値を採用するだけで、保存・復元・削除はいずれも実行しない。 */
  function adopt(): void {
    if (!currentFacts.current || !ready || unknown || !query.data?.projects[0]) return
    currentFacts.current = false
    onAdopt(query.data.projects[0])
  }
  /** 人が current facts の限界を確認した場合だけ、新しい操作の選択を許可する。 */
  function acknowledge(): void {
    if (!currentFacts.current || !ready || !unknown || !checkedRef.current) return
    currentFacts.current = false
    checkedRef.current = false
    onAcknowledge()
  }

  return <section className="panel projectReview" data-project-unknown={unknown ? '' : undefined} data-project-conflict={!unknown ? '' : undefined}>
    <h2>{unknown ? messages.projectManagement.unknownTitle : messages.projectManagement.conflictTitle}</h2>
    <p>{unknown ? messages.projectManagement.unknownHint : messages.projectManagement.conflictHint}</p>
    {intent.original ? <ProjectFacts project={intent.original} /> : <ProjectDraftFacts draft={intent.draft} action={intent.action} />}
    <button className="secondaryButton" type="button" data-project-reconcile="" disabled={started && query.pending} onClick={read}>{messages.projectManagement.readOriginal}</button>
    {started && query.pending && <p role="status">{messages.account.busy}</p>}
    <ProjectResponseNotice failure={query.failure} />
    {ready && query.data && <div data-project-reviewed="">
      <h3 tabIndex={-1} ref={factsHeading}>{messages.projectManagement.current}</h3>
      {intent.action === 'create'
        ? query.data.projects.length ? query.data.projects.map((project) => <ProjectFacts key={project.project_id} project={project} />) : <p>{messages.projectManagement.noKeyMatch}</p>
        : <ProjectComparison intent={intent} current={query.data.projects[0] ?? null} />}
      <p className="hint">{messages.projectManagement.factLimit}</p>
      {unknown ? <>
        <label className="projectAcknowledgment"><input type="checkbox" data-project-acknowledged="" checked={checked}
          onChange={(event) => { checkedRef.current = event.target.checked; setChecked(event.target.checked) }} />{messages.projectManagement.acknowledgeCheck}</label>
        <button className="secondaryButton" type="button" data-project-acknowledge="" disabled={!checked} onClick={acknowledge}>{messages.projectManagement.acknowledge}</button>
      </> : query.data.projects[0] && <button className="secondaryButton" type="button" data-project-adopt="" onClick={adopt}>{messages.projectManagement.adopt}</button>}
    </div>}
    {!unknown && <button className="secondaryButton" type="button" data-project-cancel="" onClick={onCancel}>{messages.elements.cancel}</button>}
  </section>
}
