import type { Dispatch, FormEvent, SetStateAction } from 'react'
import type { IntegrationRecord, SecretReferenceRecord } from '../api'
import { useMessages } from '../i18n'
import { COMMON_REDMINE_FIELD_KEYS, PROVIDER_FORMS, RESOURCE_PROVIDERS, SCOPE_WILDCARD,
  mcpPermissionsForAccess, mcpToolNames, supportsMcpCatalog,
  type RepositoryWriteMode, type ResourceProvider } from '../lib/resourceConfig'
import { NEW_CREDENTIAL, PROVIDER_LABELS, emptyConnectDraft, type ConnectDraft } from '../lib/resourceDrafts'
import { SecretResolverFields } from './ResourceFormFields'

/** Provider 別の接続入力を同じ草稿として表示する。通信と中間成功は親 controller が所有する。 */
export function ResourceConnectionForm({ connectDraft, setConnectDraft, secrets, editingIntegration,
  connectWriteEnabled, mcpToolsEnabled, busy, error, submitConnect, discoverTools }: {
  connectDraft: ConnectDraft
  setConnectDraft: Dispatch<SetStateAction<ConnectDraft>>
  secrets: SecretReferenceRecord[]
  editingIntegration: IntegrationRecord | null
  connectWriteEnabled: boolean
  mcpToolsEnabled: boolean
  busy: string | null
  error: string | null
  submitConnect: (event: FormEvent<HTMLFormElement>) => Promise<void>
  discoverTools: () => Promise<void>
}) {
  const messages = useMessages()
  const connectForm = PROVIDER_FORMS[connectDraft.provider]
  const connectSecrets = secrets.filter((item) => item.status === 'ACTIVE' && item.provider === connectDraft.provider)
  const selectedConnectSecret = secrets.find((item) => item.secret_reference_id === connectDraft.credentialChoice)
  return (
            <form className="resourceForm" onSubmit={(event) => void submitConnect(event)}>
              <label>{messages.resources.providerLabel}
                <select
                  disabled={editingIntegration !== null}
                  value={connectDraft.provider}
                  onChange={(event) => setConnectDraft(
                    emptyConnectDraft(event.target.value as ResourceProvider),
                  )}
                >
                  {RESOURCE_PROVIDERS.map((provider) => (
                    <option key={provider} value={provider}>{PROVIDER_LABELS[provider]}</option>
                  ))}
                </select>
              </label>
              <label>{messages.resources.nameLabel}
                <input
                  maxLength={200}
                  required
                  value={connectDraft.name}
                  onChange={(event) => setConnectDraft((value) => ({ ...value, name: event.target.value }))}
                />
              </label>
              {connectDraft.provider === 'postgres' ? (
                <>
                  <label>{messages.resources.databaseHost}<input required maxLength={253} value={connectDraft.host}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, host: event.target.value }))} /></label>
                  <label>{messages.resources.databasePort}<input required type="number" min={1} max={65535} value={connectDraft.port}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, port: event.target.value }))} /></label>
                  <label>{messages.resources.databaseName}<input required maxLength={253} value={connectDraft.database}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, database: event.target.value }))} /></label>
                  <label>{messages.resources.databaseUser}<input required maxLength={253} value={connectDraft.username}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, username: event.target.value }))} /></label>
                  <label>{messages.resources.databaseTls}<select value={connectDraft.sslmode}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, sslmode: event.target.value }))}>
                    <option value="verify-full">{messages.resources.tlsVerifyFull}</option>
                    <option value="require">{messages.resources.tlsRequire}</option>
                    <option value="disable">{messages.resources.tlsDisable}</option>
                  </select></label>
                </>
              ) : connectDraft.provider === 'mcp' ? (
                <label>{messages.resources.mcpServerUrl}<input type="url" required maxLength={2048}
                  placeholder="https://mcp.example.com/mcp" value={connectDraft.serverUrl}
                  onChange={(event) => setConnectDraft((value) => ({ ...value, serverUrl: event.target.value, mcpCatalog: null, mcpTools: false }))} /></label>
              ) : connectDraft.provider === 'redmine' ? (
                <label>{messages.resources.baseUrlLabel}
                  <input
                    className="mono"
                    placeholder="https://redmine.example.com"
                    required
                    type="url"
                    value={connectDraft.baseUrl}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, baseUrl: event.target.value }))}
                  />
                </label>
              ) : (
                <>
                  <label>{messages.resources.repositoryUriLabel}
                    <input
                      className="mono"
                      placeholder="https://repository.example.com/project"
                      required
                      value={connectDraft.repositoryUri}
                      onChange={(event) => setConnectDraft((value) => ({ ...value, repositoryUri: event.target.value }))}
                    />
                  </label>
                  <label>{messages.resources.defaultRevisionLabel}
                    <input
                      className="mono"
                      value={connectDraft.defaultRevision}
                      onChange={(event) => setConnectDraft((value) => ({ ...value, defaultRevision: event.target.value }))}
                    />
                  </label>
                  {connectDraft.access === 'read_write' && (
                    <>
                      <label>{messages.resources.writeModeLabel}
                        <select
                          value={connectDraft.writeMode}
                          onChange={(event) => setConnectDraft((value) => ({
                            ...value,
                            writeMode: event.target.value as RepositoryWriteMode,
                          }))}
                        >
                          <option value="direct">{messages.resources.writeModeDirect}</option>
                          <option value="branch">{messages.resources.writeModeBranch}</option>
                        </select>
                      </label>
                      <p className="hint">{connectDraft.writeMode === 'direct'
                        ? messages.resources.writeModeDirectHint
                        : messages.resources.writeModeBranchHint}</p>
                      {connectDraft.writeMode === 'branch' && (
                        <label>{messages.resources.writeBranchPrefixLabel}
                          <input
                            className="mono"
                            placeholder="skillmind/"
                            value={connectDraft.writeBranchPrefix}
                            onChange={(event) => setConnectDraft((value) => ({ ...value, writeBranchPrefix: event.target.value }))}
                          />
                        </label>
                      )}
                    </>
                  )}
                </>
              )}
              <label>{messages.resources.credentialLabel}
                <select
                  required={connectForm.requiresSecret || (connectDraft.provider === 'mcp' && connectDraft.mcpTools && connectDraft.access === 'read_write')}
                  value={connectDraft.credentialChoice}
                  onChange={(event) => setConnectDraft((value) => ({ ...value, credentialChoice: event.target.value, secretValue: '', locator: '' }))}
                >
                  {!connectForm.requiresSecret && <option value="" disabled={connectDraft.provider === 'mcp' && connectDraft.mcpTools && connectDraft.access === 'read_write'}>{messages.resources.notUsed}</option>}
                  <option value={NEW_CREDENTIAL}>{messages.resources.credentialNew}</option>
                  {connectSecrets.map((item) => (
                    <option key={item.secret_reference_id} value={item.secret_reference_id}>{item.name}</option>
                  ))}
                </select>
              </label>
              {selectedConnectSecret && (
                <SecretResolverFields editing provider={connectDraft.provider}
                  resolver={selectedConnectSecret.resolver} locator={connectDraft.locator}
                  secretValue={connectDraft.secretValue}
                  envExample={connectForm.environmentLocatorExample} fileExample={connectForm.fileLocatorExample}
                  onResolver={() => undefined}
                  onLocator={(locator) => setConnectDraft((value) => ({ ...value, locator }))}
                  onSecretValue={(secretValue) => setConnectDraft((value) => ({ ...value, secretValue }))}
                />
              )}
              {connectDraft.credentialChoice === NEW_CREDENTIAL && (
                <>
                  <SecretResolverFields
                    provider={connectDraft.provider}
                    resolver={connectDraft.resolver}
                    locator={connectDraft.locator}
                    secretValue={connectDraft.secretValue}
                    envExample={connectForm.environmentLocatorExample}
                    fileExample={connectForm.fileLocatorExample}
                    onResolver={(resolver) => setConnectDraft((value) => ({ ...value, resolver }))}
                    onLocator={(locator) => setConnectDraft((value) => ({ ...value, locator }))}
                    onSecretValue={(secretValue) => setConnectDraft((value) => ({ ...value, secretValue }))}
                  />
                  <p className="hint resourceWarning">{connectDraft.resolver === 'MANAGED' ? messages.resources.secretValueHint : messages.resources.secretHint}</p>
                </>
              )}
              {connectWriteEnabled && connectForm.writeCapability !== null && (
                <fieldset className="scopePicker">
                  <legend>{messages.resources.accessLabel}</legend>
                  <label className="scopeOption">
                    <input
                      checked={connectDraft.access === 'read'}
                      name="connect-access"
                      type="radio"
                      onChange={() => setConnectDraft((value) => ({ ...value, access: 'read' }))}
                    />
                    <span>{messages.resources.accessRead}</span>
                  </label>
                  <label className="scopeOption">
                    <input
                      checked={connectDraft.access === 'read_write'}
                      name="connect-access"
                      type="radio"
                      onChange={() => setConnectDraft((value) => ({ ...value, access: 'read_write' }))}
                    />
                    <span>{messages.resources.accessReadWrite}</span>
                  </label>
                  <p className="hint">{messages.resources.accessHint}</p>
                </fieldset>
              )}
              {connectDraft.provider === 'postgres' ? (
                <><fieldset className="scopePicker"><legend>{messages.resources.databaseTables}</legend>
                  <label className="scopeOption"><input type="checkbox"
                    checked={connectDraft.tables === SCOPE_WILDCARD}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, tables: event.target.checked ? SCOPE_WILDCARD : '' }))} />
                    <span>{messages.resources.databaseAllTables}</span>
                  </label>
                  {connectDraft.tables !== SCOPE_WILDCARD && <label>{messages.resources.scopeExplicitValues}
                    <textarea required className="mono compactTextarea"
                      placeholder={'public.reports\npublic.items'} value={connectDraft.tables}
                      onChange={(event) => setConnectDraft((value) => ({ ...value, tables: event.target.value }))} />
                  </label>}
                  <span className="hint">{connectDraft.tables === SCOPE_WILDCARD || connectDraft.writeColumns === SCOPE_WILDCARD
                    ? messages.resources.databaseAllHint : connectWriteEnabled && connectDraft.access === 'read_write'
                    ? messages.resources.databaseWriteHint : messages.resources.databaseReadHint}</span></fieldset>
                {connectWriteEnabled && connectDraft.access === 'read_write' && <>
                  <fieldset className="scopePicker"><legend>{messages.resources.databaseWriteColumns}</legend>
                    <label className="scopeOption"><input type="checkbox"
                      checked={connectDraft.writeColumns === SCOPE_WILDCARD}
                      onChange={(event) => setConnectDraft((value) => ({ ...value, writeColumns: event.target.checked ? SCOPE_WILDCARD : '' }))} />
                      <span>{messages.resources.databaseAllColumns}</span>
                    </label>
                    {connectDraft.writeColumns !== SCOPE_WILDCARD && <label>{messages.resources.scopeExplicitValues}
                      <textarea required className="mono compactTextarea"
                        placeholder={'public.reports.id\npublic.reports.status'} value={connectDraft.writeColumns}
                        onChange={(event) => setConnectDraft((value) => ({ ...value, writeColumns: event.target.value }))} />
                    </label>}
                  </fieldset>
                  <fieldset className="scopePicker"><legend>{messages.resources.databaseOperations}</legend>
                    {['INSERT', 'UPDATE'].map((operation) => <label className="scopeOption" key={operation}>
                      <input type="checkbox" checked={connectDraft.databaseOperations.includes(operation)}
                        onChange={(event) => setConnectDraft((value) => ({ ...value, databaseOperations: event.target.checked
                          ? [...value.databaseOperations, operation] : value.databaseOperations.filter((item) => item !== operation) }))} />
                      <span>{operation}</span>
                    </label>)}
                  </fieldset>
                </>}</>
              ) : connectDraft.provider === 'mcp' ? (
                <>
                {mcpToolsEnabled && <div className="resourceFormSection">
                  <button className="secondaryButton" type="button" disabled={busy !== null || editingIntegration === null}
                    onClick={() => void discoverTools()}>{messages.resources.mcpDiscover}</button>
                  {editingIntegration === null && <p className="hint">{messages.resources.mcpSaveFirst}</p>}
                  {connectDraft.mcpCatalog && <>
                    <label className="scopeOption"><input type="checkbox" checked={connectDraft.mcpTools} disabled={!supportsMcpCatalog(connectDraft.mcpCatalog)}
                      onChange={(event) => setConnectDraft((value) => ({ ...value, mcpTools: event.target.checked }))} />
                      <span>{messages.resources.mcpEnableTools}</span></label>
                    <p className="hint">{messages.resources.mcpToolsHint}</p>
                    {mcpToolNames(connectDraft.mcpCatalog).map((name) => {
                      const mode = connectDraft.mcpTools ? mcpPermissionsForAccess(connectDraft.mcpCatalog, connectDraft.access)[name] : undefined
                      return <div key={name} className="resourceToolPermission">
                        <code>{name}</code>
                        <span>{mode === 'read' ? messages.resources.mcpToolRead : mode === 'call' ? messages.resources.mcpToolCall : messages.resources.mcpToolDenied}</span>
                      </div>
                    })}
                  </>}
                </div>}
                <label>{messages.resources.mcpResourceUris}<textarea className="mono compactTextarea"
                  value={connectDraft.resourceUris}
                  onChange={(event) => setConnectDraft((value) => ({ ...value, resourceUris: event.target.value }))} />
                  <span className="hint">{messages.resources.mcpReadHint}</span></label>
                </>
              ) : connectDraft.provider === 'redmine' ? (
                <>
                  <fieldset className="scopePicker">
                    <legend>{messages.resources.issueScopeLabel}</legend>
                    <label className="scopeOption">
                      <input
                        checked={connectDraft.issueScope === 'all'}
                        name="connect-issue-scope"
                        type="radio"
                        onChange={() => setConnectDraft((value) => ({ ...value, issueScope: 'all' }))}
                      />
                      <span>{messages.resources.issueScopeAllOption}</span>
                    </label>
                    <label className="scopeOption">
                      <input
                        checked={connectDraft.issueScope === 'list'}
                        name="connect-issue-scope"
                        type="radio"
                        onChange={() => setConnectDraft((value) => ({ ...value, issueScope: 'list' }))}
                      />
                      <span>{messages.resources.issueScopeListOption}</span>
                    </label>
                    {connectDraft.issueScope === 'list' && (
                      <>
                        <label>{messages.resources.issueIdsLabel}
                          <textarea
                            className="mono compactTextarea"
                            placeholder={'1001\n1002'}
                            required
                            value={connectDraft.issueIds}
                            onChange={(event) => setConnectDraft((value) => ({ ...value, issueIds: event.target.value }))}
                          />
                        </label>
                        <p className="hint">{messages.resources.issueIdsHint}</p>
                      </>
                    )}
                  </fieldset>
                  {connectDraft.access === 'read_write' && (
                    <fieldset className="scopePicker">
                      <legend>{messages.resources.fieldScopeLabel}</legend>
                      <label className="scopeOption">
                        <input
                          checked={connectDraft.fieldScope === 'all'}
                          name="connect-field-scope"
                          type="radio"
                          onChange={() => setConnectDraft((value) => ({ ...value, fieldScope: 'all' }))}
                        />
                        <span>{messages.resources.fieldScopeAllOption}</span>
                      </label>
                      <label className="scopeOption">
                        <input
                          checked={connectDraft.fieldScope === 'list'}
                          name="connect-field-scope"
                          type="radio"
                          onChange={() => setConnectDraft((value) => ({ ...value, fieldScope: 'list' }))}
                        />
                        <span>{messages.resources.fieldScopeListOption}</span>
                      </label>
                      {connectDraft.fieldScope === 'list' && (
                        <>
                          {COMMON_REDMINE_FIELD_KEYS.map((fieldKey) => (
                            <label className="scopeOption" key={fieldKey}>
                              <input
                                checked={connectDraft.fieldKeys.includes(fieldKey)}
                                type="checkbox"
                                onChange={(event) => setConnectDraft((value) => ({
                                  ...value,
                                  fieldKeys: event.target.checked
                                    ? [...value.fieldKeys, fieldKey]
                                    : value.fieldKeys.filter((item) => item !== fieldKey),
                                }))}
                              />
                              <span className="mono">{fieldKey}</span>
                            </label>
                          ))}
                          <p className="hint">{messages.resources.fieldKeysHint}</p>
                          <label>{messages.resources.customFieldKeysLabel}
                            <input
                              className="mono"
                              value={connectDraft.customFieldKeys}
                              onChange={(event) => setConnectDraft((value) => ({ ...value, customFieldKeys: event.target.value }))}
                            />
                          </label>
                        </>
                      )}
                    </fieldset>
                  )}
                </>
              ) : (
                <>
                  <label>{messages.resources.pathsLabel}
                    <textarea
                      className="mono compactTextarea"
                      placeholder={'src\ndocs'}
                      required
                      value={connectDraft.paths}
                      onChange={(event) => setConnectDraft((value) => ({ ...value, paths: event.target.value }))}
                    />
                  </label>
                  <label>{messages.resources.revisionsLabel}
                    <textarea
                      className="mono compactTextarea"
                      value={connectDraft.revisions}
                      onChange={(event) => setConnectDraft((value) => ({ ...value, revisions: event.target.value }))}
                    />
                  </label>
                </>
              )}
              {error && <p className="error" role="alert">{error}</p>}
              <button className="primaryButton" disabled={busy !== null} type="submit">
                {editingIntegration === null ? messages.resources.connectSubmit : messages.resources.save}
              </button>
            </form>
  )
}
