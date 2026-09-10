import { useCallback, useEffect, useState, type FormEvent } from 'react'

import {
  changeMyPassword, loadMyAccount, loadUserAccount, revokeMySessions, revokeUserSessions, updateUser,
  type AuthSessionRecord, type UserAccountRecord, type UserMutationRecord, type UserRole, type UserStatus,
} from '../api'
import { useUserMutation, useUserQuery, type SessionEnded } from '../hooks/useUserRequest'
import { useMessages } from '../i18n'
import { PASSWORD_MIN_LENGTH, passwordIssue, sameUser, type UserFailure } from '../lib/userFeedback'
import { UserAccountFacts, UserResponseNotice } from './UserAccountElements'
import { UserSecurityEvents } from './UserSecurityEvents'

/** 編集の権限は mode では付与せず、Server の actor 検証に委ねる。 */
interface AccountPanelProps {
  userId: string
  own: boolean
  session: AuthSessionRecord
  revision?: number
  onSessionEnded: SessionEnded
  onChanged: (account: UserAccountRecord) => void
}

/** 対象または会話の変更は草稿・password・request の lifecycle を分離する。 */
export function UserAccountPanel(props: AccountPanelProps) {
  return <AccountEditor key={`${props.userId}:${props.own}:${props.session.csrf_token}`} {...props} />
}

/** 一つの原版を保ち、競合/unknown 後の最新読取を明示採用する編集器。 */
function AccountEditor({ userId, own, session, revision = 0, onSessionEnded, onChanged }: AccountPanelProps) {
  const messages = useMessages().account
  const loader = useCallback(async (signal: AbortSignal) => {
    const account = own ? await loadMyAccount(signal) : await loadUserAccount(userId, signal)
    if (!sameUser(account.user_id, userId)) throw new Error('Unexpected account target')
    return account
  }, [own, userId])
  const query = useUserQuery(`${own}:${userId}:${revision}`, loader, onSessionEnded)
  const mutation = useUserMutation(onSessionEnded)
  const [base, setBase] = useState<UserAccountRecord | null>(null)
  const [name, setName] = useState('')
  const [role, setRole] = useState<UserRole>('USER')
  const [status, setStatus] = useState<UserStatus>('ACTIVE')
  const [currentPassword, setCurrentPassword] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [passwordError, setPasswordError] = useState<'passwordPolicy' | 'passwordMismatch' | 'invalidRequest' | null>(null)
  const [confirmRevoke, setConfirmRevoke] = useState(false)
  const [confirmChange, setConfirmChange] = useState(false)
  const [reviewAfter, setReviewAfter] = useState(0)
  const [unknownVersion, setUnknownVersion] = useState<number | null>(null)
  const [success, setSuccess] = useState<UserMutationRecord | null>(null)
  const [auditRevision, setAuditRevision] = useState(0)

  useEffect(() => {
    if (base || !query.data || query.pending || query.failure) return
    setBase(query.data)
    setName(query.data.display_name)
    setRole(query.data.system_role)
    setStatus(query.data.status)
  }, [base, query.data, query.pending, query.failure])

  const needsReview = mutation.failure?.key === 'versionConflict' || mutation.failure?.key === 'unknown'
    || (base !== null && query.data !== null && query.data.row_version > base.row_version)
  const canWrite = base !== null && !mutation.busy && !mutation.cooldown && !query.pending && !query.failure && !needsReview
  const latest = query.data && base && !query.pending && !query.failure
    && query.completed >= reviewAfter && query.data.row_version >= base.row_version ? query.data : null

  /** 入力の原値は送信中の一回だけに渡し、失敗/離頁から再生しない。 */
  const clearSensitive = (): void => {
    setCurrentPassword(''); setPassword(''); setConfirmation(''); setPasswordError(null)
    setConfirmRevoke(false); setConfirmChange(false); setSuccess(null)
  }
  /** 成功した自己失効は Server の logout を追加せず App の会話だけを閉じる。 */
  const completed = (result: UserMutationRecord): void => {
    if (result.session_revoked) { onSessionEnded('revoked'); return }
    setBase(result.user)
    setName(result.user.display_name); setRole(result.user.system_role); setStatus(result.user.status)
    setSuccess(result); setAuditRevision((value) => value + 1)
    onChanged(result.user)
    query.refresh()
  }
  /** 新しい資料を読んでも元の版/草稿を上書きせず、別の比較として保持する。 */
  const rejected = (reason: UserFailure): void => {
    if (reason.key !== 'versionConflict' && reason.key !== 'unknown') return
    if (reason.key === 'unknown') setUnknownVersion(base?.row_version ?? null)
    setReviewAfter(query.revision + 1)
    query.refresh()
    setAuditRevision((value) => value + 1)
  }
  /** 本人 endpoint の応答も現在対象と照合してから共通成功経路へ渡す。 */
  const perform = (operation: (signal: AbortSignal) => Promise<UserMutationRecord>): void => {
    if (!canWrite) return
    if (mutation.submit(async (signal) => {
      const result = await operation(signal)
      if (!sameUser(result.user.user_id, userId)) throw new Error('Unexpected mutation target')
      return result
    }, completed, rejected)) clearSensitive()
  }
  /** 原版に対する password 変更。ブラウザ検証を迂回した submit も再確認する。 */
  const changePassword = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    if (!canWrite || !base) return
    const issue = passwordIssue(password, confirmation)
    if (issue) { setPasswordError(issue); return }
    if (!currentPassword || new TextEncoder().encode(currentPassword).length > 1024) {
      setPasswordError('invalidRequest'); return
    }
    const input = { current_password: currentPassword, new_password: password, expected_row_version: base.row_version }
    perform((signal) => changeMyPassword(input, session.csrf_token, signal))
  }
  /** 名前と明示的な権限/状態だけを元の expected version で送る。 */
  const save = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    if (!canWrite || !base || !name.trim() || (role !== base.system_role || status !== base.status) && !confirmChange) return
    const input = { display_name: name.trim(), system_role: role, status, expected_row_version: base.row_version }
    perform((signal) => updateUser(userId, input, session.csrf_token, signal))
  }
  /** 最新版の採用は user 操作だけに限定し、草稿や unknown の過去事実は消さない。 */
  const adopt = (): void => {
    if (!latest || mutation.busy) return
    setBase(latest)
    mutation.acknowledge()
    setConfirmChange(false); setConfirmRevoke(false)
  }

  return <div className="accountEditor">
    <section className="panel" aria-busy={query.pending || mutation.busy}>
      <div className="panelHeader"><h2>{own ? messages.myAccount : messages.edit}</h2>
        <button className="secondaryButton" type="button" disabled={query.pending || mutation.busy} onClick={query.refresh}>{messages.refresh}</button>
      </div>
      <UserResponseNotice failure={query.failure} />
      <UserResponseNotice failure={mutation.failure} cooldown={mutation.cooldown} />
      {(query.pending || mutation.busy) && <p role="status">{messages.busy}</p>}
      {success && <p role="status">{messages.mutationSuccess} {messages.revokedCount(success.revoked_sessions)}</p>}
      {base && <>
        <UserAccountFacts account={base} compact />
        {unknownVersion !== null && <p className="accountNotice" role="status">{messages.unknownHint} {messages.versionUsed(unknownVersion)}</p>}
        {needsReview && <section className="accountComparison" aria-label={messages.reviewTitle}>
          <h3>{messages.reviewTitle}</h3><p>{messages.reviewHint}</p>
          {latest && <><p>{messages.latestVersion(latest.row_version)}</p><UserAccountFacts account={latest} /></>}
          <button className="secondaryButton" disabled={!latest || mutation.busy} type="button" onClick={adopt}>{messages.adoptLatest}</button>
        </section>}
        {own ? <details className="detailDisclosure accountAction" data-account-action="password">
          <summary>{messages.changePassword}</summary>
          <form className="accountForm" data-account-form="password" onSubmit={changePassword}>
          <p className="hint">{messages.passwordHint}</p>
          <fieldset disabled={!canWrite}>
            <label>{messages.currentPassword}<input autoComplete="current-password" type="password" required maxLength={1024} value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></label>
            <label>{messages.newPassword}<input autoComplete="new-password" type="password" required minLength={PASSWORD_MIN_LENGTH} maxLength={1024} value={password} onChange={(event) => setPassword(event.target.value)} /></label>
            <label>{messages.confirmPassword}<input autoComplete="new-password" type="password" required minLength={PASSWORD_MIN_LENGTH} maxLength={1024} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
            <p className="hint">{messages.passwordPolicy}</p>
            {passwordError && <p role="alert" className="error">{passwordError === 'invalidRequest' ? messages.failures.invalidRequest : messages[passwordError]}</p>}
            <button className="primaryButton" type="submit">{messages.changePassword}</button>
          </fieldset>
        </form></details> : <form className="accountForm" data-account-form="edit" onSubmit={save}>
          <p className="hint">{messages.draftMemoryOnly}</p>
          <fieldset disabled={!canWrite}>
            <label>{messages.fields.name}<input required maxLength={200} value={name} onChange={(event) => { setName(event.target.value); setConfirmChange(false) }} /></label>
            <label>{messages.fields.role}<select aria-label={messages.fields.role} value={role} onChange={(event) => { setRole(event.target.value === 'ADMIN' ? 'ADMIN' : 'USER'); setConfirmChange(false) }}>
              <option value="USER">{messages.roles.USER}</option><option value="ADMIN">{messages.roles.ADMIN}</option>
            </select></label>
            <label>{messages.fields.status}<select aria-label={messages.fields.status} value={status} onChange={(event) => { setStatus(event.target.value === 'ACTIVE' ? 'ACTIVE' : 'DISABLED'); setConfirmChange(false) }}>
              <option value="ACTIVE">{messages.statuses.ACTIVE}</option><option value="DISABLED">{messages.statuses.DISABLED}</option>
            </select></label>
            {(role !== base.system_role || status !== base.status) && <label className="accountCheckbox"><input type="checkbox" required checked={confirmChange} onChange={(event) => setConfirmChange(event.target.checked)} />{messages.confirmChange}</label>}
            <button className="primaryButton" type="submit">{messages.save}</button>
          </fieldset>
        </form>}
        <details className="detailDisclosure accountDanger accountAction" data-account-action="sessions">
          <summary>{messages.revoke}</summary><p>{messages.revokeHint}</p>
          <label className="accountCheckbox"><input type="checkbox" disabled={!canWrite} checked={confirmRevoke} onChange={(event) => setConfirmRevoke(event.target.checked)} />{messages.confirmRevoke}</label>
          <button className="dangerButton" type="button" disabled={!canWrite || !confirmRevoke} onClick={() => {
            if (!confirmRevoke) return
            perform((signal) => own ? revokeMySessions(base.row_version, session.csrf_token, signal) : revokeUserSessions(userId, base.row_version, session.csrf_token, signal))
          }}>{messages.revoke}</button>
        </details>
      </>}
    </section>
    {base && <UserSecurityEvents userId={userId} own={own} revision={auditRevision + revision} onSessionEnded={onSessionEnded} />}
  </div>
}
