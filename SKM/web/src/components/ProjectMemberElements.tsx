import { useEffect, useRef } from 'react'

import type { ProjectMemberRecord, ProjectRecord, UserAccountRecord } from '../api'
import { useMessages } from '../i18n'
import type { ProjectMemberFailure } from '../lib/projectMemberFeedback'
import { formatLocalTimestamp } from '../lib/presentation'
import { sameUser } from '../lib/userFeedback'

/** 確認画面と未知結果の核対が保持する、変更不能な元の操作対象。 */
export interface MemberIntent {
  action: 'add' | 'remove'
  project: Pick<ProjectRecord, 'project_id' | 'name' | 'key'>
  target: Pick<UserAccountRecord, 'user_id' | 'email' | 'display_name'>
  before: ProjectMemberRecord | null
}

/** Server 本文ではなく固定分類だけを表示し、失敗箇所へ焦点を案内する。 */
export function MemberResponseNotice({ failure }: { failure: ProjectMemberFailure | null }) {
  const messages = useMessages().projectMembers
  const notice = useRef<HTMLDivElement>(null)
  useEffect(() => { if (failure) notice.current?.focus() }, [failure])
  return failure && <div className="memberNotice error" role="alert" tabIndex={-1} ref={notice}>{messages.failures[failure.key]}</div>
}

/** 関係の ACTIVE/REMOVED を account の有効・無効へ読み替えず表示する。 */
export function ProjectMemberList({ members, locked, onRemove }: {
  members: ProjectMemberRecord[]
  locked: boolean
  onRemove: (member: ProjectMemberRecord) => void
}) {
  const messages = useMessages()
  if (members.length === 0) return <p>{messages.projectMembers.emptyMembers}</p>
  return <ul className="memberList" aria-label={messages.projectMembers.membersTitle}>
    {members.map((member) => <li className="memberCard" data-member-id={member.user_id} key={member.user_id}>
      <div>
        <strong>{member.display_name}</strong><p>{member.email}</p><p className="mono">{member.user_id}</p>
        <dl className="memberFacts">
          <div><dt>{messages.projectMembers.relationship}</dt><dd>{messages.projectMembers.states[member.status]}</dd></div>
          <div><dt>{messages.projectMembers.joinedAt}</dt><dd><time dateTime={member.joined_at}>{formatLocalTimestamp(member.joined_at)}</time></dd></div>
        </dl>
      </div>
      {member.status === 'ACTIVE' && <button className="dangerButton" type="button" data-member-action="remove"
        aria-label={`${messages.projectMembers.remove}: ${member.email}`} disabled={locked} onClick={() => onRemove(member)}>{messages.projectMembers.remove}</button>}
    </li>)}
  </ul>
}

/** 全候補を表示し、無効 account や既参加 user を一覧から黙って落とさない。 */
export function ProjectMemberCandidateList({ accounts, members, locked, onAdd }: {
  accounts: UserAccountRecord[]
  members: ProjectMemberRecord[]
  locked: boolean
  onAdd: (account: UserAccountRecord) => void
}) {
  const messages = useMessages()
  if (accounts.length === 0) return <p>{messages.projectMembers.emptyCandidates}</p>
  return <ul className="memberList" aria-label={messages.projectMembers.candidatesTitle}>
    {accounts.map((account) => {
      const joined = members.some((member) => sameUser(member.user_id, account.user_id) && member.status === 'ACTIVE')
      return <li className="memberCard" data-member-candidate={account.user_id} key={account.user_id}>
        <div>
          <strong>{account.display_name}</strong><p>{account.email}</p><p className="mono">{account.user_id}</p>
          <dl className="memberFacts">
            <div><dt>{messages.account.fields.role}</dt><dd>{messages.account.roles[account.system_role]}</dd></div>
            <div><dt>{messages.account.fields.status}</dt><dd>{messages.account.statuses[account.status]}</dd></div>
          </dl>
          {joined && <p className="hint">{messages.projectMembers.alreadyMember}</p>}
          {account.status === 'DISABLED' && <p className="hint">{messages.projectMembers.disabledCandidate}</p>}
        </div>
        <button className="secondaryButton" type="button" data-member-action="add" aria-label={`${messages.projectMembers.add}: ${account.email}`}
          disabled={locked || joined || account.status === 'DISABLED'} onClick={() => onAdd(account)}>{messages.projectMembers.add}</button>
      </li>
    })}
  </ul>
}

/** 元の project/account/関係を固定表示し、一覧更新で確認対象を差し替えない。 */
export function MemberIntentFacts({ intent }: { intent: MemberIntent }) {
  const messages = useMessages()
  return <div className="memberOriginal">
    <strong>{intent.project.name} · {intent.project.key}</strong><p className="mono">{intent.project.project_id}</p>
    <p>{intent.target.display_name} · {intent.target.email}</p><p className="mono">{intent.target.user_id}</p>
    <dl className="memberFacts">
      <div><dt>{messages.projectMembers.originalAction}</dt><dd>{messages.projectMembers[intent.action]}</dd></div>
      <div><dt>{messages.projectMembers.originalRelationship}</dt><dd>{messages.projectMembers.states[intent.before?.status ?? 'ABSENT']}</dd></div>
    </dl>
  </div>
}

/** Native confirm に依存せず、精確対象と影響を読んだ後の一回だけ送信する。 */
export function MemberIntentConfirmation({ intent, disabled, changed, onConfirm, onCancel }: {
  intent: MemberIntent
  disabled: boolean
  changed: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const messages = useMessages()
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { heading.current?.focus() }, [])
  return <section className="memberIntent" data-member-intent="" aria-label={messages.projectMembers.intentTitle}>
    <h3 tabIndex={-1} ref={heading}>{messages.projectMembers.intentTitle}</h3>
    <p>{intent.action === 'add' ? messages.projectMembers.confirmAdd : messages.projectMembers.confirmRemove}</p>
    <MemberIntentFacts intent={intent} />
    {changed && <p role="status">{messages.projectMembers.selectionChanged}</p>}
    <div className="inlineActions">
      <button className="primaryButton" type="button" data-member-confirm="" disabled={disabled || changed} onClick={onConfirm}>{messages.projectMembers.confirm}</button>
      <button className="secondaryButton" type="button" data-member-cancel="" disabled={disabled} onClick={onCancel}>{messages.elements.cancel}</button>
    </div>
  </section>
}
