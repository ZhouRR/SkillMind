import { useCallback, useLayoutEffect, useRef, useState } from 'react'

import { loadProjectMembers, loadUserAccount } from '../api'
import { useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { PROJECT_MEMBER_REQUEST_POLICY } from '../lib/projectMemberFeedback'
import { sameUser } from '../lib/userFeedback'
import { MemberIntentFacts, MemberResponseNotice, type MemberIntent } from './ProjectMemberElements'

/** 未知 write は再送せず、元 Project の関係と元 account の二つを独立読取で核対する。 */
export function ProjectMemberReview({ intent, enabled, onSessionEnded, onAcknowledge }: {
  intent: MemberIntent
  enabled: boolean
  onSessionEnded: SessionEnded
  onAcknowledge: () => void
}) {
  const messages = useMessages()
  const [started, setStarted] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)
  const acknowledgedRef = useRef(false)
  const loader = useCallback(async (signal: AbortSignal) => {
    const [members, account] = await Promise.all([
      loadProjectMembers(intent.project.project_id, signal),
      loadUserAccount(intent.target.user_id, signal),
    ])
    return { member: members.find((member) => sameUser(member.user_id, intent.target.user_id)) ?? null, account }
  }, [intent])
  const query = useResourceQuery(JSON.stringify([intent.project.project_id, intent.target.user_id]), loader,
    onSessionEnded, PROJECT_MEMBER_REQUEST_POLICY, enabled && started)
  const ready = enabled && started && !query.pending && !query.failure && query.completed >= 0 && !!query.data

  useLayoutEffect(() => {
    if (enabled) return
    acknowledgedRef.current = false
    setAcknowledged(false)
  }, [enabled])

  /** 再読取を始めた瞬間に旧確認を消し、古い facts への同 tick acknowledgment を防ぐ。 */
  function reconcile(): void {
    if (!enabled) return
    acknowledgedRef.current = false
    setAcknowledged(false)
    setStarted(true)
    query.refresh()
  }

  /** 読取成功は自動解除せず、独立した人の確認だけが次の選択を許可する。 */
  function acknowledge(): void {
    if (!ready || !acknowledgedRef.current) return
    acknowledgedRef.current = false
    onAcknowledge()
  }

  return <section className="memberUnknown" data-member-unknown="" aria-label={messages.projectMembers.unknownTitle}>
    <h3>{messages.projectMembers.unknownTitle}</h3>
    <p>{messages.projectMembers.unknownHint}</p>
    <MemberIntentFacts intent={intent} />
    <button className="secondaryButton" type="button" data-member-reconcile="" disabled={!enabled || started && query.pending} onClick={reconcile}>{messages.projectMembers.reconcile}</button>
    <p className="hint">{messages.projectMembers.reconcileHint}</p>
    {started && query.pending && <p role="status">{messages.account.busy}</p>}
    <MemberResponseNotice failure={query.failure} />
    {ready && query.data && <div className="memberReviewFacts" data-member-reviewed="" aria-busy={query.pending}>
      <dl className="memberFacts">
        <div><dt>{messages.projectMembers.currentRelationship}</dt><dd>{messages.projectMembers.states[query.data.member?.status ?? 'ABSENT']}</dd></div>
        <div><dt>{messages.account.fields.email}</dt><dd>{query.data.account.email}</dd></div>
        <div><dt>{messages.account.fields.userId}</dt><dd>{query.data.account.user_id}</dd></div>
        <div><dt>{messages.account.fields.role}</dt><dd>{messages.account.roles[query.data.account.system_role]}</dd></div>
        <div><dt>{messages.account.fields.status}</dt><dd>{messages.account.statuses[query.data.account.status]}</dd></div>
      </dl>
      <label className="memberAcknowledgment"><input type="checkbox" data-member-acknowledged="" checked={acknowledged} disabled={!ready}
        onChange={(event) => { acknowledgedRef.current = event.target.checked; setAcknowledged(event.target.checked) }} />{messages.projectMembers.acknowledgeCheck}</label>
      <button className="secondaryButton" type="button" data-member-acknowledge="" disabled={!ready || !acknowledged} onClick={acknowledge}>{messages.projectMembers.acknowledge}</button>
    </div>}
  </section>
}
