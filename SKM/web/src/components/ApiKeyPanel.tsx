import { useCallback, useState } from 'react'

import { createApiKey, loadApiKeys, revokeApiKey, type AuthSessionRecord, type CreatedApiKeyRecord } from '../api'
import { useUserMutation, useUserQuery, type SessionEnded } from '../hooks/useUserRequest'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'

/** 秘密はこの mount のメモリだけに置き、再取得・自動再発行を提供しない。 */
export function ApiKeyPanel({ session, onSessionEnded }: { session: AuthSessionRecord; onSessionEnded: SessionEnded }) {
  const messages = useMessages()
  const text = messages.apiKeys
  const [name, setName] = useState('')
  const [created, setCreated] = useState<CreatedApiKeyRecord | null>(null)
  const [confirmed, setConfirmed] = useState<string | null>(null)
  const loader = useCallback((signal: AbortSignal) => loadApiKeys(signal), [])
  const query = useUserQuery('api-keys', loader, onSessionEnded)
  const mutation = useUserMutation(onSessionEnded)
  const unknown = mutation.failure?.key === 'unknown'
  // Unknown の後は必ず新しい一覧を読んでから人が再操作を選ぶ。
  const [reviewRevision, setReviewRevision] = useState<number | null>(null)
  const canReview = reviewRevision !== null && query.completed >= reviewRevision && !query.pending && !query.failure
  const busy = mutation.busy || unknown

  return <section className="apiKeyPanel" aria-label={text.title}>
    <p className="muted">{text.scope}</p>
    <form className="apiKeyCreate" onSubmit={(event) => {
      event.preventDefault()
      if (created || !name.trim()) return
      mutation.submit((signal) => createApiKey(name.trim(), session.csrf_token, signal), (value) => {
        setCreated(value); setName(''); query.refresh()
      }, (failure) => { if (failure.key === 'unknown') setReviewRevision(query.revision + 1) })
    }}>
      <label className="field"><span>{text.name}</span>
        <input value={name} maxLength={200} required disabled={busy || created !== null}
          onChange={(event) => setName(event.target.value)} autoComplete="off" />
      </label>
      <button className="primaryButton" disabled={busy || created !== null || !name.trim()}>{mutation.busy ? messages.account.busy : text.create}</button>
    </form>
    {created && <div className="apiKeySecret" role="status">
      <strong>{created.api_key.name}</strong>
      <p>{text.once}</p>
      <label className="field"><span>API Key</span><input readOnly value={created.token} autoComplete="off" spellCheck={false}
        onFocus={(event) => event.currentTarget.select()} /></label>
      <code>Authorization: Bearer &lt;API_KEY&gt;</code>
      <button className="secondaryButton" onClick={() => setCreated(null)}>{text.saved}</button>
    </div>}
    {mutation.failure && <p role="alert">{unknown ? text.unknown : messages.account.failures[mutation.failure.key]}</p>}
    <div className="buttonRow">
      <button className="secondaryButton" disabled={query.pending || mutation.busy} onClick={query.refresh}>{messages.account.refresh}</button>
      {unknown && <button className="secondaryButton" disabled={!canReview} onClick={() => { mutation.acknowledge(); setReviewRevision(null) }}>{text.reviewed}</button>}
    </div>
    {query.failure && <p role="alert">{messages.account.failures[query.failure.key]}</p>}
    {query.pending && <p aria-live="polite">{messages.account.busy}</p>}
    {query.data?.length === 0 && <p className="muted">{text.empty}</p>}
    <div className="apiKeyList">{query.data?.map((key) => <article className="apiKeyRow" key={key.id}>
      <div className="apiKeyInfo"><strong>{key.name}</strong><code>{key.key_prefix}…</code>
        <span>{key.revoked_at ? text.revoked : text.active}</span>
        <span className="muted">{text.created}: {formatLocalTimestamp(key.created_at)}</span>
        <span className="muted">{text.lastUsed}: {key.last_used_at ? formatLocalTimestamp(key.last_used_at) : text.never}</span>
      </div>
      {!key.revoked_at && (confirmed === key.id ? <div className="apiKeyRevoke">
        <span>{text.confirm}</span><button className="destructiveButton compactButton" disabled={busy} onClick={() => {
          mutation.submit((signal) => revokeApiKey(key.id, session.csrf_token, signal), () => {
            if (created?.api_key.id === key.id) setCreated(null)
            setConfirmed(null); query.refresh()
          }, (failure) => { if (failure.key === 'unknown') setReviewRevision(query.revision + 1) })
        }}>{text.revoke}</button>
        <button className="secondaryButton compactButton" disabled={busy} onClick={() => setConfirmed(null)}>{messages.elements.cancel}</button>
      </div> : <button className="secondaryButton" disabled={busy} onClick={() => setConfirmed(key.id)}>{text.revoke}</button>)}
    </article>)}</div>
  </section>
}
