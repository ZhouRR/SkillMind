import { useCallback, useState, type FormEvent } from 'react'

import { loadUsers, type ProjectMemberRecord, type UserAccountRecord } from '../api'
import { useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { PROJECT_MEMBER_REQUEST_POLICY } from '../lib/projectMemberFeedback'
import { UserPager } from './UserAccountElements'
import { MemberResponseNotice, ProjectMemberCandidateList } from './ProjectMemberElements'

/** 検索と offset を Server に渡し、候補の一部を全組織の結果と見せない。 */
export function ProjectMemberCandidates({ projectId, enabled, members, locked, onAdd, onSessionEnded }: {
  projectId: string
  enabled: boolean
  members: ProjectMemberRecord[]
  locked: boolean
  onAdd: (account: UserAccountRecord) => void
  onSessionEnded: SessionEnded
}) {
  const messages = useMessages()
  const [draft, setDraft] = useState('')
  const [search, setSearch] = useState({ text: '', offset: 0 })
  const limit = 25
  const loader = useCallback((signal: AbortSignal) => loadUsers(search.text, limit, search.offset, signal), [search])
  const query = useResourceQuery(JSON.stringify([projectId, search]), loader, onSessionEnded, PROJECT_MEMBER_REQUEST_POLICY, enabled)

  /** Enter も URL 遷移させず、明示された検索だけを先頭 page から読む。 */
  function submitSearch(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    if (!enabled || [...draft].length > 200) return
    setSearch({ text: draft.trim(), offset: 0 })
  }

  return <section className="memberCandidates" aria-busy={query.pending}>
    <div className="panelHeader"><h3>{messages.projectMembers.candidatesTitle}</h3>
      <button className="secondaryButton" type="button" disabled={!enabled || query.pending} onClick={query.refresh}>{messages.account.refresh}</button>
    </div>
    <p className="hint">{messages.projectMembers.searchHint}</p>
    <form className="memberSearch" data-member-search="" onSubmit={submitSearch}>
      <label>{messages.projectMembers.search}<input type="search" name="q" autoComplete="off" maxLength={200} value={draft}
        disabled={!enabled} onChange={(event) => setDraft(event.target.value)} /></label>
      <button className="secondaryButton" type="submit" disabled={!enabled}>{messages.projectMembers.search}</button>
    </form>
    <MemberResponseNotice failure={query.failure} />
    {query.pending && <p role="status">{messages.account.busy}</p>}
    {!query.pending && !query.failure && query.data && <>
      <ProjectMemberCandidateList accounts={query.data.items} members={members} locked={locked || query.pending || !!query.failure} onAdd={onAdd} />
      <UserPager offset={search.offset} limit={limit} count={query.data.items.length} total={query.data.total}
        pending={!enabled || query.pending || !!query.failure} onChange={(offset) => setSearch((current) => ({ ...current, offset }))} />
    </>}
  </section>
}
