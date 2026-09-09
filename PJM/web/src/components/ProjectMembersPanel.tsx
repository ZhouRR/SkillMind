import { useCallback, useLayoutEffect, useRef, useState } from 'react'

import { addProjectMember, loadProjectMembers, removeProjectMember, type AuthSessionRecord, type ProjectMemberRecord, type ProjectRecord, type UserAccountRecord } from '../api'
import { useResourceMutation, useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { PROJECT_MEMBER_REQUEST_POLICY } from '../lib/projectMemberFeedback'
import { sameUser } from '../lib/userFeedback'
import { EmptyState } from './PageElements'
import { ProjectMemberCandidates } from './ProjectMemberCandidates'
import { MemberIntentConfirmation, MemberIntentFacts, MemberResponseNotice, ProjectMemberList, type MemberIntent } from './ProjectMemberElements'
import { ProjectMemberReview } from './ProjectMemberReview'
import '../styles/project-members.css'

/** Actor/会話/Project が変わると元要求を隔離し、同対象の再読取では未知状態を残す。 */
export function ProjectMembersPanel({ projectContextId, currentProject, session, onSessionEnded }: {
  projectContextId: string
  currentProject?: ProjectRecord | null
  session: AuthSessionRecord
  onSessionEnded: SessionEnded
}) {
  const messages = useMessages()
  if (session.user.system_role !== 'ADMIN') return <EmptyState text={messages.projectMembers.adminOnly} />
  return <MembersContent key={`${session.user.user_id}:${session.csrf_token}:${projectContextId.toLowerCase()}`}
    projectId={projectContextId.toLowerCase()} currentProject={currentProject} session={session} onSessionEnded={onSessionEnded} />
}

/** 同一 Project の参加関係を読む・確認する・変更する一つの書込境界。 */
function MembersContent({ projectId, currentProject, session, onSessionEnded }: {
  projectId: string
  currentProject?: ProjectRecord | null
  session: AuthSessionRecord
  onSessionEnded: SessionEnded
}) {
  const messages = useMessages()
  const authorized = !!currentProject && currentProject.project_id.toLowerCase() === projectId
  const authorization = useRef(authorized)
  authorization.current = authorized
  const ended = useRef(onSessionEnded)
  ended.current = onSessionEnded
  const notifySessionEnded = useCallback<SessionEnded>((reason) => {
    if (authorization.current) ended.current(reason)
  }, [])
  const loader = useCallback((signal: AbortSignal) => loadProjectMembers(projectId, signal), [projectId])
  const members = useResourceQuery(projectId, loader, notifySessionEnded, PROJECT_MEMBER_REQUEST_POLICY, authorized)
  const mutation = useResourceMutation(notifySessionEnded, PROJECT_MEMBER_REQUEST_POLICY)
  const [intent, setIntent] = useState<MemberIntent | null>(null)
  const intentRef = useRef<MemberIntent | null>(null)
  const [uncertain, setUncertain] = useState<MemberIntent | null>(null)
  const uncertainRef = useRef<MemberIntent | null>(null)
  const [previousUnknown, setPreviousUnknown] = useState<MemberIntent | null>(null)
  const [success, setSuccess] = useState(false)
  const submitted = useRef(false)
  const listHeading = useRef<HTMLHeadingElement>(null)
  const ready = authorized && !members.pending && !members.failure && members.data !== null

  /** 同対象の認可再確認も、すでに発行した write の rollback とは見なさない。 */
  useLayoutEffect(() => {
    if (authorized) return
    mutation.interrupt()
    if (submitted.current && intentRef.current) {
      uncertainRef.current = intentRef.current
      setUncertain(intentRef.current)
      submitted.current = false
    }
  }, [authorized, mutation.interrupt])

  /** 一覧の行をそのまま次の操作へ差し替えず、確認時点の対象を複製する。 */
  function choose(action: MemberIntent['action'], target: MemberIntent['target']): void {
    if (!ready || !currentProject || intentRef.current || uncertainRef.current || submitted.current) return
    const before = members.data?.find((member) => sameUser(member.user_id, target.user_id)) ?? null
    if (action === 'add' && (currentProject.status !== 'ACTIVE' || before?.status === 'ACTIVE')) return
    if (action === 'remove' && before?.status !== 'ACTIVE') return
    const next: MemberIntent = {
      action,
      project: { project_id: currentProject.project_id, name: currentProject.name, key: currentProject.key },
      target: { user_id: target.user_id, email: target.email, display_name: target.display_name },
      before: before ? { ...before } : null,
    }
    intentRef.current = next
    setIntent(next)
    setSuccess(false)
  }

  /** 無効 account は表示しても、候補行や古い event から追加させない。 */
  function add(account: UserAccountRecord): void {
    if (account.status === 'ACTIVE') choose('add', account)
  }

  /** 送信前に行の可否だけを再確認し、原対象や原操作を自動変換しない。 */
  function eligible(original: MemberIntent): boolean {
    if (!ready || original.project.project_id.toLowerCase() !== projectId) return false
    const relationship = members.data?.find((member) => sameUser(member.user_id, original.target.user_id))
    return original.action === 'add'
      ? currentProject?.status === 'ACTIVE' && relationship?.status !== 'ACTIVE'
      : relationship?.status === 'ACTIVE'
  }

  /** submit の戻り値と同期 ref で、同 tick の二重確認・取消を止める。 */
  function confirm(): void {
    const original = intentRef.current
    if (!original || submitted.current || uncertainRef.current || !eligible(original)) return
    const accepted = mutation.submit(async (signal) => {
      if (original.action === 'add') {
        await addProjectMember(original.project.project_id, original.target.user_id, session.csrf_token, signal)
      } else {
        await removeProjectMember(original.project.project_id, original.target.user_id, session.csrf_token, signal)
      }
    }, () => {
      submitted.current = false
      if (!authorization.current) {
        uncertainRef.current = original
        setUncertain(original)
        return
      }
      intentRef.current = null
      setIntent(null)
      setSuccess(true)
      members.refresh()
      listHeading.current?.focus()
    }, (failure) => {
      submitted.current = false
      if (failure.key === 'unknown') {
        uncertainRef.current = original
        setUncertain(original)
      }
    })
    if (accepted) submitted.current = true
  }

  /** 未送信の確認だけを取り消し、送信中や未知の元要求を捨てない。 */
  function cancel(): void {
    if (submitted.current || uncertainRef.current) return
    intentRef.current = null
    setIntent(null)
    listHeading.current?.focus()
  }

  /** 両読取後の人の確認だけが新選択を許可し、元の write は再実行しない。 */
  function acknowledge(): void {
    if (!authorized || !uncertainRef.current || submitted.current) return
    setPreviousUnknown(uncertainRef.current)
    uncertainRef.current = null
    intentRef.current = null
    setUncertain(null)
    setIntent(null)
    mutation.acknowledge()
    members.refresh()
    listHeading.current?.focus()
  }

  return <section className="panel projectMembersPanel" data-project-members="" data-member-context={authorized ? 'ready' : 'unavailable'}>
    {!authorized && <EmptyState text={messages.projectMembers.needProject} />}
    {/* 認可確認中は操作集合を隠して読取も止めるが、同対象の未知要求と草稿は破棄しない。 */}
    <div className="projectMembersContent" hidden={!authorized}>
      <div className="panelHeader"><h2>{messages.projectMembers.title}</h2></div>
      <div className="memberProjectIdentity"><strong>{currentProject?.name} · {currentProject?.key}</strong><p className="mono">{projectId}</p></div>
      <p>{messages.projectMembers.description}</p>
      <p className="memberBoundaryHint">{messages.projectMembers.adminBypass}</p>
      {currentProject?.status === 'ARCHIVED' && <p className="memberBoundaryHint">{messages.projectMembers.archivedHint}</p>}
      <MemberResponseNotice failure={mutation.failure} />
      {success && <p role="status">{messages.projectMembers.saved}</p>}
      {previousUnknown && <details className="memberPreviousUnknown"><summary>{messages.projectMembers.acknowledgedHint}</summary><MemberIntentFacts intent={previousUnknown} /></details>}
      {uncertain
        ? <ProjectMemberReview intent={uncertain} enabled={authorized} onSessionEnded={notifySessionEnded} onAcknowledge={acknowledge} />
        : intent && <MemberIntentConfirmation intent={intent} disabled={mutation.busy} changed={!eligible(intent)} onConfirm={confirm} onCancel={cancel} />}
      <section aria-busy={members.pending}>
        <div className="panelHeader"><h3 ref={listHeading} tabIndex={-1}>{messages.projectMembers.membersTitle}</h3>
          <button className="secondaryButton" type="button" data-member-refresh="" disabled={!authorized || members.pending || mutation.busy} onClick={members.refresh}>{messages.account.refresh}</button>
        </div>
        <p className="hint">{messages.projectMembers.relationshipHint}</p>
        <MemberResponseNotice failure={members.failure} />
        {members.pending && <p role="status">{messages.account.busy}</p>}
        {ready && members.data && <ProjectMemberList members={members.data} locked={!!intent || !!uncertain || mutation.busy} onRemove={(member: ProjectMemberRecord) => choose('remove', member)} />}
      </section>
      <div hidden={currentProject?.status !== 'ACTIVE'}>
        <ProjectMemberCandidates projectId={projectId} enabled={authorized && currentProject?.status === 'ACTIVE'} members={ready ? members.data ?? [] : []}
          locked={!ready || currentProject?.status !== 'ACTIVE' || !!intent || !!uncertain || mutation.busy} onAdd={add} onSessionEnded={notifySessionEnded} />
      </div>
    </div>
  </section>
}
