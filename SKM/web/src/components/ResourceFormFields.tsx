import type { ReactNode } from 'react'
import type { SecretResolver } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'
import { PROVIDER_FORMS, type ResourceProvider, type ScopeDraftEntry } from '../lib/resourceConfig'
import type { ResourceTab } from '../lib/resourceDrafts'
import { ActionMenu } from './ActionMenu'

/** 資源設定タブの button。選択状態を aria-selected で表し、tablist 内で切り替える。 */
export function ResourceTabButton({ current, tab, onSelect, children }: {
  current: ResourceTab
  tab: ResourceTab
  onSelect: (tab: ResourceTab) => void
  children: ReactNode
}) {
  return (
    <button
      aria-selected={current === tab}
      className="tab"
      onClick={() => onSelect(tab)}
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
  provider, resolver, locator, secretValue, envExample, fileExample, onResolver, onLocator, onSecretValue, editing = false,
}: {
  editing?: boolean
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
        <select disabled={editing} value={resolver} onChange={(event) => onResolver(event.target.value as SecretResolver)}>
          <option value="ENVIRONMENT">{messages.resources.resolverEnvOption}</option>
          <option value="FILE">{messages.resources.resolverFileOption}</option>
          <option value="MANAGED">{messages.resources.resolverManagedOption}</option>
        </select>
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
export function ResourceList({ items, emptyText, busy }: { busy: boolean; emptyText?: string; items: Array<{
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
            {item.onEdit && <button disabled={busy} className="secondaryButton" onClick={item.onEdit} type="button">{messages.resources.edit}</button>}
            {item.onDisable && !item.onEdit && !item.onDelete
              ? <button disabled={busy} className="secondaryButton" onClick={item.onDisable} type="button">{messages.resources.disable}</button>
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
  const update = (index: number, patch: Partial<ScopeDraftEntry>): void => {
    onChange(entries.map((entry, at) => (at === index ? { ...entry, ...patch } : entry)))
  }
  return (
    <fieldset className="scopePicker">
      <legend>{legend}</legend>
      {entries.map((entry, index) => (
        <div className="scopePickerGroup" key={entry.key}>
          <p className="resourceGroupLabel">{entry.key}</p>
          {entry.wildcardSource ? (
            <>
              {allowWildcard && (
                <label className="scopeOption">
                  <input
                    checked={entry.keepAll}
                    type="checkbox"
                    onChange={(event) => update(index, { keepAll: event.target.checked })}
                  />
                  <span>{messages.resources.scopeKeepAllOption}</span>
                </label>
              )}
              {(!allowWildcard || !entry.keepAll) && (
                <>
                  <input
                    className="mono"
                    value={entry.raw}
                    onChange={(event) => update(index, { raw: event.target.value })}
                  />
                  <p className="hint">{messages.resources.scopeNarrowHint}</p>
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
        </div>
      ))}
    </fieldset>
  )
}
