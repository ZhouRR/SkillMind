import { useCallback, useState, type FormEvent } from 'react'

import { createUser, loadUsers, type AuthSessionRecord, type UserAccountRecord, type UserRole } from '../api'
import { useUserMutation, useUserQuery, type SessionEnded } from '../hooks/useUserRequest'
import { useMessages } from '../i18n'
import { creationEmailQuery, PASSWORD_MIN_LENGTH, passwordIssue } from '../lib/userFeedback'
import { UserPager, UserResponseNotice, UserSummaryList } from './UserAccountElements'

/** 新規作成には再送契約がない。unknown は元 email の確認と明示的な別意図に分ける。 */
export function UserCreatePanel({ session, onSessionEnded, onCreated, onSelect }: {
  session: AuthSessionRecord
  onSessionEnded: SessionEnded
  onCreated: (account: UserAccountRecord) => void
  onSelect: (userId: string) => void
}) {
  const messages = useMessages().account
  const mutation = useUserMutation(onSessionEnded)
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [role, setRole] = useState<UserRole | ''>('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [issue, setIssue] = useState<'passwordPolicy' | 'passwordMismatch' | 'invalidRequest' | null>(null)
  const [unknownEmail, setUnknownEmail] = useState<string | null>(null)
  const [pastUnknownEmail, setPastUnknownEmail] = useState<string | null>(null)
  const [review, setReview] = useState(false)
  const [success, setSuccess] = useState<UserAccountRecord | null>(null)

  /** Password はこの一回だけに渡し、拒否/unknown 後にも非機密な入力のみ残す。 */
  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    if (mutation.busy || mutation.cooldown || unknownEmail !== null) return
    const passwordError = passwordIssue(password, confirmation)
    if (passwordError) { setIssue(passwordError); return }
    if (!email.trim() || !name.trim() || !role) { setIssue('invalidRequest'); return }
    const input = { email: email.trim(), display_name: name.trim(), system_role: role, password }
    if (!mutation.submit((signal) => createUser(input, session.csrf_token, signal), (result) => {
      if (result.session_revoked) { onSessionEnded('revoked'); return }
      setSuccess(result.user)
      setEmail(''); setName(''); setRole('')
      onCreated(result.user)
    }, (failure) => {
      if (failure.key === 'unknown') {
        setUnknownEmail(input.email)
        setPastUnknownEmail(input.email)
        setReview(false)
      }
    })) return
    setPassword(''); setConfirmation(''); setIssue(null); setSuccess(null)
  }

  return <section className="panel accountCreate" data-account-create="" aria-busy={mutation.busy}>
    <h2>{messages.create}</h2><p className="hint">{messages.createHint}</p>
    <UserResponseNotice failure={mutation.failure} cooldown={mutation.cooldown} />
    {mutation.busy && <p role="status">{messages.busy}</p>}
    {success && <>
      <p role="status">{messages.createdSuccess}</p>
      <UserSummaryList users={[success]} disabled={false} onSelect={onSelect} />
    </>}
    {pastUnknownEmail !== null && unknownEmail === null && <p className="accountNotice" role="status">
      {messages.unknownHint} {messages.creationOriginalEmail}: {pastUnknownEmail}
    </p>}
    <form className="accountForm" data-account-form="create" onSubmit={submit}>
      <fieldset disabled={mutation.busy || !!mutation.cooldown || unknownEmail !== null}>
        <label>{messages.fields.email}<input type="email" autoComplete="off" required maxLength={320}
          value={email} onChange={(event) => setEmail(event.target.value)} /></label>
        <label>{messages.fields.name}<input required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label>{messages.fields.role}<select aria-label={messages.fields.role} required value={role} onChange={(event) => setRole(event.target.value === 'ADMIN' ? 'ADMIN' : event.target.value === 'USER' ? 'USER' : '')}>
          <option value="">{messages.rolePlaceholder}</option><option value="USER">{messages.roles.USER}</option><option value="ADMIN">{messages.roles.ADMIN}</option>
        </select></label>
        <label>{messages.initialPassword}<input type="password" autoComplete="new-password" required minLength={PASSWORD_MIN_LENGTH} maxLength={1024}
          value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <label>{messages.confirmPassword}<input type="password" autoComplete="new-password" required minLength={PASSWORD_MIN_LENGTH} maxLength={1024}
          value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
        <p className="hint">{messages.passwordPolicy}</p>
        {issue && <p role="alert" className="error">{issue === 'invalidRequest' ? messages.failures.invalidRequest : messages[issue]}</p>}
        <button type="submit" className="primaryButton">{messages.create}</button>
      </fieldset>
    </form>
    {unknownEmail !== null && <section className="accountCreateReview" aria-label={messages.checkCreation}>
      <p role="status">{messages.unknownHint}</p>
      <p>{messages.creationOriginalEmail}: <span>{unknownEmail}</span></p>
      <p className="hint">{messages.creationReviewHint}</p>
      {!review && <button type="button" className="secondaryButton" onClick={() => setReview(true)}>{messages.checkCreation}</button>}
      {review && <CreationReview email={unknownEmail} onSessionEnded={onSessionEnded} onSelect={onSelect} onReviewed={() => {
        mutation.acknowledge()
        setUnknownEmail(null)
        setReview(false)
      }} />}
    </section>}
  </section>
}

/** 原 email の検索結果も現在資料だけであり、元の作成成功を合成しない。 */
function CreationReview({ email, onSessionEnded, onSelect, onReviewed }: {
  email: string
  onSessionEnded: SessionEnded
  onSelect: (userId: string) => void
  onReviewed: () => void
}) {
  const messages = useMessages().account
  const [offset, setOffset] = useState(0)
  const limit = 25
  const loader = useCallback((signal: AbortSignal) => loadUsers(creationEmailQuery(email), limit, offset, signal), [email, offset])
  const query = useUserQuery(`${email}:${offset}`, loader, onSessionEnded)
  return <>
    <button type="button" className="secondaryButton" disabled={query.pending} onClick={query.refresh}>{messages.checkCreation}</button>
    <UserResponseNotice failure={query.failure} />
    {query.pending && <p role="status">{messages.busy}</p>}
    {query.data && <>
      <UserSummaryList users={query.data.items} disabled={query.pending || !!query.failure} onSelect={onSelect} />
      <UserPager offset={offset} limit={limit} count={query.data.items.length} total={query.data.total}
        pending={query.pending || !!query.failure} onChange={setOffset} />
    </>}
    <button type="button" className="secondaryButton" disabled={!query.data || query.pending || !!query.failure}
      onClick={onReviewed}>{messages.beginNewCreate}</button>
  </>
}
