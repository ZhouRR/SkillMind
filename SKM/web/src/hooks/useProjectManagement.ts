import { useRef, useState } from 'react'

import { archiveProject, createProject, deleteProject, unarchiveProject, updateProject, type AuthSessionRecord, type ProjectRecord } from '../api'
import { PROJECT_REQUEST_POLICY } from '../lib/projectFeedback'
import { projectCreateInput, projectDraft, projectIntent, projectUpdateInput, validProjectDraft, type ProjectDraft, type ProjectIntent } from '../lib/projectManagement'
import { useResourceMutation, type SessionEnded } from './useResourceRequest'

/** 五つの Project write は一つの同期門禁を共有し、元要求と編集草稿を別に保持する。 */
export function useProjectManagement({ session, onSessionEnded, onSaved }: {
  session: AuthSessionRecord
  onSessionEnded: SessionEnded
  onSaved: (action: ProjectIntent['action'], project: ProjectRecord) => void
}) {
  const mutation = useResourceMutation(onSessionEnded, PROJECT_REQUEST_POLICY)
  const [editor, setEditor] = useState<{ original: ProjectRecord | null; draft: ProjectDraft }>({ original: null, draft: projectDraft() })
  const [intent, setIntent] = useState<ProjectIntent | null>(null)
  const [phase, setPhase] = useState<'confirm' | 'conflict' | 'unknown'>('confirm')
  const activeIntent = useRef<ProjectIntent | null>(null)
  const currentPhase = useRef(phase)
  const submitted = useRef(false)
  const [previousUnknown, setPreviousUnknown] = useState<ProjectIntent | null>(null)
  const [saved, setSaved] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const locked = !!intent || mutation.busy

  /** 選択を変更できるのは未送信時だけ。別行から未知要求を解除させない。 */
  function edit(project: ProjectRecord | null): boolean {
    if (session.user.system_role !== 'ADMIN' || activeIntent.current || submitted.current) return false
    setEditor({ original: project ? structuredClone(project) : null, draft: projectDraft(project) })
    setSaved(false)
    setAttempted(false)
    return true
  }
  /** 入力は未送信草稿だけを変更し、凍結済み payload を変更しない。 */
  function change(draft: ProjectDraft): void {
    if (activeIntent.current || submitted.current) return
    setEditor((old) => ({ ...old, draft }))
  }
  /** 確認画面を開く時点で行・版・入力を固定し、送信は別の操作にする。 */
  function choose(action: ProjectIntent['action'], project: ProjectRecord | null = editor.original): void {
    if (session.user.system_role !== 'ADMIN' || activeIntent.current || submitted.current) return
    if (action !== 'create' && !project) return
    const draft = action === 'create' || action === 'edit' ? editor.draft : projectDraft(project)
    if ((action === 'create' || action === 'edit') && !validProjectDraft(draft, action === 'create')) return
    const next = projectIntent(action, project, draft)
    activeIntent.current = next
    currentPhase.current = 'confirm'
    setIntent(next)
    setPhase('confirm')
    setSaved(false)
    setAttempted(false)
  }
  /** 同 tick の二重確認も、共用 hook と元 intent ref の両方で拒否する。 */
  function confirm(): void {
    const candidate = activeIntent.current
    if (!candidate || currentPhase.current !== 'confirm' || submitted.current) return
    // 新版採用後の別送信は、その送信版を原事実とする。以前の拒否版と混同しない。
    const original: ProjectIntent = { ...candidate, original: candidate.base }
    const accepted = mutation.submit(async (signal) => {
      const { action, base, draft } = original
      if (action === 'create') return createProject(projectCreateInput(draft), session.csrf_token, signal)
      if (!base) throw new Error('Project operation requires its original identity')
      if (action === 'edit') return updateProject(base.project_id, projectUpdateInput(original), session.csrf_token, signal)
      if (action === 'archive') return archiveProject(base.project_id, base.row_version, session.csrf_token, signal)
      if (action === 'restore') return unarchiveProject(base.project_id, base.row_version, session.csrf_token, signal)
      await deleteProject(base.project_id, base.row_version, session.csrf_token, signal)
      return base
    }, (project) => {
      submitted.current = false
      activeIntent.current = null
      setIntent(null)
      setSaved(true)
      setEditor({ original: null, draft: projectDraft() })
      onSaved(original.action, project)
    }, (failure) => {
      submitted.current = false
      if (failure.key === 'unknown' || failure.key === 'versionConflict') {
        const next = failure.key === 'unknown' ? 'unknown' : 'conflict'
        currentPhase.current = next
        setPhase(next)
      }
    })
    if (accepted) {
      submitted.current = true
      activeIntent.current = original
      setIntent(original)
      setAttempted(true)
    }
  }
  /** 既知拒否/未送信確認だけを閉じ、元草稿を消さない。 */
  function cancel(): void {
    if (submitted.current || currentPhase.current === 'unknown') return
    activeIntent.current = null
    setIntent(null)
    mutation.acknowledge()
  }
  /** 人が選んだ新しい基準版だけを採用し、草稿を変更せず送信も行わない。 */
  function adopt(project: ProjectRecord): void {
    const original = activeIntent.current
    if (!original?.base || submitted.current || currentPhase.current !== 'conflict'
      || original.base.project_id.toLowerCase() !== project.project_id.toLowerCase()) return
    const next = { ...original, base: structuredClone(project) }
    activeIntent.current = next
    currentPhase.current = 'confirm'
    setIntent(next)
    setPhase('confirm')
    if (original.action === 'edit') setEditor({ original: structuredClone(project), draft: { ...original.draft } })
    mutation.acknowledge()
  }
  /** 読取事実の明示的確認は門禁だけを解除し、同 key 作成や原 write を自動再送しない。 */
  function acknowledge(): void {
    if (!activeIntent.current || submitted.current || currentPhase.current !== 'unknown') return
    setPreviousUnknown(activeIntent.current)
    activeIntent.current = null
    currentPhase.current = 'confirm'
    setIntent(null)
    setPhase('confirm')
    setEditor({ original: null, draft: projectDraft() })
    mutation.acknowledge()
  }
  return { editor, intent, phase, previousUnknown, saved, locked, failure: attempted ? mutation.failure : null, mutation, edit, change, choose, confirm, cancel, adopt, acknowledge }
}
