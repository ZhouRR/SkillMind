import { handleTabKeyDown } from './TabButton'
import { Select } from './Select'
import { useId, type ReactNode } from 'react'
import type { SecretResolver } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'
import { PROVIDER_FORMS, type ResourceProvider, type ScopeDraftEntry } from '../lib/resourceConfig'
import type { ResourceTab } from '../lib/resourceDrafts'
import { ActionMenu } from './ActionMenu'

/** 資源設定タブの button。選択状態を aria-selected で表し、tablist 内で切り替える。 */
export function ResourceTabButton({ current, tab, onSelect, children, id, panelId }: {
  id: string
  panelId: string
  current: ResourceTab
  tab: ResourceTab
  onSelect: (tab: ResourceTab) => void
  children: ReactNode
}) {
  return (
    <button
      id={id}
      aria-controls={panelId}
      aria-selected={current === tab}
      tabIndex={current === tab ? 0 : -1}
      className="tab"
      onClick={() => onSelect(tab)}
      onKeyDown={handleTabKeyDown}
      role="tab"
      type="button"
    >
      {children}
    </button>
  )
}

/** resolver 選択と、MANAGED の明文入力 / ENVIRONMENT・FILE の locator 入力を切り替える共通 field。
 *  接続 form と凭据 form の両方で使い、MANAGED 分岐の重複を一箇所へ集約する(hint は各 form 側が持つ)。 */
export function SecretResolverFields({
  provider, resolver, locator, secretValue, envExample, fileExample, onResolver, onLocator, onSecretValue, editing = false, disabled = false,
}: {
  editing?: boolean
  disabled?: boolean
  provider: ResourceProvider
  resolver: SecretResolver
  locator: string
  secretValue: string
  envExample: string
  fileExample: string
  onResolver: (resolver: SecretResolver) => void
  onLocator: (locator: string) => void
  onSecretValue: (secretValue: string) => void
}) {
  const messages = useMessages()
  return (
    <>
      <label>{messages.resources.resolverLabel}
        <Select disabled={editing || disabled} value={resolver} onValueChange={(nextValue) => onResolver(nextValue as SecretResolver)}>
          <option value="ENVIRONMENT">{messages.resources.resolverEnvOption}</option>
          <option value="FILE">{messages.resources.resolverFileOption}</option>
          <option value="MANAGED">{messages.resources.resolverManagedOption}</option>
        </Select>
      </label>
      {resolver === 'MANAGED' ? (
        <label>{messages.resources.credentialValueLabels[PROVIDER_FORMS[provider].credentialKind]}
          <input
            type="password"
            autoComplete="off"
            placeholder={editing ? messages.resources.keepSecret : messages.resources.credentialValuePlaceholders[PROVIDER_FORMS[provider].credentialKind]}
            required={!editing}
            value={secretValue}
            onChange={(event) => onSecretValue(event.target.value)}
          />
        </label>
      ) : (
        <label>{messages.resources.locatorFieldLabel}
          <input
            className="mono"
            placeholder={editing ? messages.resources.keepSecret : resolver === 'ENVIRONMENT' ? envExample : fileExample}
            required={!editing}
            value={locator}
            onChange={(event) => onLocator(event.target.value)}
          />
        </label>
      )}
    </>
  )
}

/** 管理資源の編集・無効化・参照保護付き削除を表示する。 */
export function ResourceList({ items, emptyText, busy, loaded = true }: { loaded?: boolean; busy: boolean; emptyText?: string; items: Array<{
  id: string
  title: string
  detail: string
  status: string
  updatedAt: string
  onDisable?: () => void
  onEdit?: () => void
  onDelete?: () => void
}> }) {
  const messages = useMessages()
  if (!loaded) return null
  if (items.length === 0) return <p className="compactEmpty">{emptyText ?? messages.resources.notConfigured}</p>
  return (
    <ul className="resourceList">
      {items.map((item) => (
        <li className="resourceItem" key={item.id}>
          <div className="resourceInfo">
            <strong>{item.title}</strong>
            <span className="resourceValue">{item.detail}</span>
            <small>{messages.enums.resourceStatus[item.status] ?? item.status} · {formatLocalTimestamp(item.updatedAt)}</small>
          </div>
          <div className="panelHeaderActions">
            {item.onEdit && <button disabled={busy} className="secondaryButton compactButton" onClick={item.onEdit} type="button">{messages.resources.edit}</button>}
            {item.onDisable && !item.onEdit && !item.onDelete
              ? <button disabled={busy} className="secondaryButton compactButton" onClick={item.onDisable} type="button">{messages.resources.disable}</button>
              : <ActionMenu label={messages.common.moreActions(item.title)} disabled={busy} items={[
                ...(item.onDisable ? [{ id: 'disable', label: messages.resources.disable, onSelect: item.onDisable }] : []),
                ...(item.onDelete ? [{ id: 'delete', label: messages.resources.delete, onSelect: item.onDelete, danger: true, separatorBefore: Boolean(item.onDisable) }] : []),
              ]} />}
          </div>
        </li>
      ))}
    </ul>
  )
}

/** Integration scope の部分集合を選ばせる。

    Server は「Integration scope と同じ key 集合 + 各値の部分集合」だけを受理する。
    explicit な key は既存値の checkbox、wildcard の key は「保持不限」または
    自由入力での収窄とし、事前許可(allowWildcard=false)では wildcard の維持を出さない。 */
export function ScopeSubsetPicker({ allowWildcard, entries, legend, onChange }: {
  allowWildcard: boolean
  entries: ScopeDraftEntry[]
  legend: string
  onChange: (next: ScopeDraftEntry[]) => void
}) {
  const messages = useMessages()
  const id = useId()
  const update = (index: number, patch: Partial<ScopeDraftEntry>): void => {
    onChange(entries.map((entry, at) => (at === index ? { ...entry, ...patch } : entry)))
  }
  return (
    <fieldset className="scopePicker">
      <legend>{legend}</legend>
      {entries.map((entry, index) => (
        <fieldset className="scopePickerGroup" key={entry.key}>
          <legend className="resourceGroupLabel">{messages.resources.scopeKeyLabels[entry.key] ?? entry.key}</legend>
          {messages.resources.scopeKeyLabels[entry.key] && <small className="mono resourceTechnicalKey">{entry.key}</small>}
          {entry.wildcardSource ? (
            <>
              {allowWildcard && (
                <label className="scopeOption">
                  <input
                    checked={entry.keepAll}
                    type="checkbox"
                    onChange={(event) => update(index, { keepAll: event.target.checked })}
                  />
                  <span>{messages.resourcesAudit.scopeKeepAllLabel(messages.resources.scopeKeyLabels[entry.key] ?? entry.key)}</span>
                </label>
              )}
              {(!allowWildcard || !entry.keepAll) && (
                <>
                  <label htmlFor={`${id}-${index}`}>{messages.resourcesAudit.scopeNarrowLabel(messages.resources.scopeKeyLabels[entry.key] ?? entry.key)}</label>
                  <input
                    id={`${id}-${index}`}
                    aria-describedby={`${id}-${index}-hint`}
                    className="mono"
                    value={entry.raw}
                    onChange={(event) => update(index, { raw: event.target.value })}
                  />
                  <p className="hint" id={`${id}-${index}-hint`}>{messages.resources.scopeNarrowHint}</p>
                </>
              )}
            </>
          ) : (
            <>
              {entry.sourceValues.length === 0 && <p className="compactEmpty">—</p>}
              {entry.sourceValues.map((value) => (
                <label className="scopeOption" key={value}>
                  <input
                    checked={entry.picked.includes(value)}
                    type="checkbox"
                    onChange={(event) => update(index, {
                      picked: event.target.checked
                        ? [...entry.picked, value]
                        : entry.picked.filter((item) => item !== value),
                    })}
                  />
                  <span className="mono">{value}</span>
                </label>
              ))}
            </>
          )}
        </fieldset>
      ))}
    </fieldset>
  )
}

/** 各 drawer と一覧が同じ待機終了・手動照合を表示する。書込の取消・再送は行わない。 */
export function ResourceRequestFeedback({ busy, unconfirmed, loading, loadError, canAcknowledge,
  stopWaiting, refresh, acknowledge }: {
  busy: string | null
  unconfirmed: { label: string } | null
  loading: boolean
  loadError: string | null
  canAcknowledge: boolean
  stopWaiting: () => void
  refresh: () => void
  acknowledge: () => void
}) {
  const messages = useMessages()
  if (busy) return <div className="resourceRequestFeedback">
    <p role="status">{messages.resourcesAudit.working}</p>
    <p className="hint">{messages.resourcesAudit.waitHint}</p>
    <button className="secondaryButton" onClick={stopWaiting} type="button">{messages.resourcesAudit.stopWaiting}</button>
  </div>
  if (!unconfirmed) return null
  return <section className="resourceRequestFeedback" aria-label={messages.resourcesAudit.unknownTitle}>
    <p className="resourceWarning" role="alert">{messages.resourcesAudit.unknownTitle}</p>
    <strong>{unconfirmed.label}</strong>
    <p>{messages.resourcesAudit.unknownHint}</p>
    {loadError && <p className="error" role="alert">{loadError}</p>}
    <p className="hint">{messages.resourcesAudit.reviewHint}</p>
    <div className="resourceFormActions">
      <button className="secondaryButton" disabled={loading} onClick={refresh} type="button">
        {loading ? messages.resources.loadingConfig : messages.resourcesAudit.refresh}
      </button>
      <button className="secondaryButton" disabled={!canAcknowledge} onClick={acknowledge} type="button">
        {messages.resourcesAudit.reviewComplete}
      </button>
    </div>
  </section>
}
