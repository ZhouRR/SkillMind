import { useCallback, useRef, useState, type FormEvent } from 'react'

import { loadUsers, type AuthSessionRecord, type UserAccountRecord } from '../api'
import { useUserQuery, type SessionEnded } from '../hooks/useUserRequest'
import { useMessages } from '../i18n'
import { UserAccountPanel } from './UserAccountPanel'
import { UserPager, UserResponseNotice, UserSummaryList } from './UserAccountElements'
import { UserCreatePanel } from './UserCreatePanel'

/** ADMIN 一覧も検索・ページ送りの正本を Server に置き、選択後は精確 ID を読む。 */
export function UserDirectory({ session, revision, onSessionEnded, onChanged }: {
  session: AuthSessionRecord
  revision: number
  onSessionEnded: SessionEnded
  onChanged: (account: UserAccountRecord) => void
}) {
  const messages = useMessages().account
  const [draft, setDraft] = useState('')
  const [search, setSearch] = useState({ text: '', offset: 0 })
  const [selected, setSelected] = useState<string | null>(null)
  const selectionHeading = useRef<HTMLHeadingElement>(null)
  const directoryHeading = useRef<HTMLHeadingElement>(null)
  const limit = 25
  const loader = useCallback((signal: AbortSignal) => loadUsers(search.text, limit, search.offset, signal), [search])
  const query = useUserQuery(JSON.stringify([search.text, search.offset, revision]), loader, onSessionEnded)

  /** 入力途中を request に流さず、明示検索のたびに先頭 page から読む。 */
  const submitSearch = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    if ([...draft].length > 200) return
    setSearch({ text: draft.trim(), offset: 0 })
  }
  /** 選択した ID を新しい editor lifecycle にし、一覧の古い版を編集原版にしない。 */
  const select = (userId: string): void => {
    setSelected(userId)
    window.requestAnimationFrame(() => selectionHeading.current?.focus())
  }
  /** 閉じた editor の応答は破棄し、keyboard の位置を一覧へ戻す。 */
  const close = (): void => {
    setSelected(null)
    directoryHeading.current?.focus()
  }
  return <div className="accountDirectory" data-account-directory="">
    <section className="panel" aria-busy={query.pending}>
      <div className="panelHeader"><h2 ref={directoryHeading} tabIndex={-1}>{messages.manageUsers}</h2>
        <button type="button" className="secondaryButton" disabled={query.pending} onClick={query.refresh}>{messages.refresh}</button>
      </div>
      <form className="accountSearch" data-account-form="search" onSubmit={submitSearch}>
        <label>{messages.search}<input type="search" maxLength={200} value={draft} placeholder={messages.searchPlaceholder}
          autoComplete="off" onChange={(event) => setDraft(event.target.value)} /></label>
        <button type="submit" className="secondaryButton">{messages.search}</button>
      </form>
      <UserResponseNotice failure={query.failure} />
      {query.pending && <p role="status">{messages.busy}</p>}
      {query.data && <>
        <UserSummaryList users={query.data.items} disabled={query.pending || !!query.failure} onSelect={select} />
        <UserPager offset={search.offset} limit={limit} count={query.data.items.length} total={query.data.total}
          pending={query.pending || !!query.failure} onChange={(offset) => setSearch((value) => ({ ...value, offset }))} />
      </>}
    </section>
    <UserCreatePanel session={session} onSessionEnded={onSessionEnded} onCreated={onChanged} onSelect={select} />
    {selected && <section data-account-editor={selected} aria-labelledby="selected-account-heading">
      <div className="panelHeader"><h2 id="selected-account-heading" tabIndex={-1} ref={selectionHeading}>{messages.edit}</h2>
        <button type="button" className="secondaryButton" onClick={close}>{messages.close}</button>
      </div>
      <UserAccountPanel userId={selected} own={false} session={session} revision={revision}
        onSessionEnded={onSessionEnded} onChanged={onChanged} />
    </section>}
  </div>
}
