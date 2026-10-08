import { Select } from './Select'
import type { Dispatch, FormEvent, SetStateAction, ReactNode } from 'react'
import type { IntegrationRecord, SecretReferenceRecord } from '../api'
import { useMessages } from '../i18n'
import { PROVIDER_FORMS, RESOURCE_PROVIDERS,
  mcpPermissionsForAccess, mcpToolNames, supportsMcpCatalog,
  type RepositoryWriteMode, type ResourceProvider } from '../lib/resourceConfig'
import { NEW_CREDENTIAL, PROVIDER_LABELS, emptyConnectDraft, type ConnectDraft } from '../lib/resourceDrafts'
import { SecretResolverFields } from './ResourceFormFields'

/** Provider 別の接続入力を同じ草稿として表示する。通信と中間成功は親 controller が所有する。 */
export function ResourceConnectionForm({ connectDraft, setConnectDraft, secrets, editingIntegration,
  connectWriteEnabled, mcpToolsEnabled, busy, error, submitConnect, discoverTools, locked, feedback, onClose }: {
  locked: boolean
  feedback: ReactNode
  onClose: () => void
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
            <form className="resourceForm" aria-busy={busy !== null} onSubmit={(event) => void submitConnect(event)}>
              <fieldset className="resourceFormFields" disabled={locked}>
              <h3 className="resourceSectionTitle">{messages.resourcesAudit.connectionSection}</h3>
              <label>{messages.resources.providerLabel}
                <Select
                  disabled={locked || editingIntegration !== null}
                  value={connectDraft.provider}
                  onValueChange={(nextValue) => setConnectDraft(
                    emptyConnectDraft(nextValue as ResourceProvider),
                  )}
                >
                  {RESOURCE_PROVIDERS.map((provider) => (
                    <option key={provider} value={provider}>{PROVIDER_LABELS[provider]}</option>
                  ))}
                </Select>
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
                  <label>{messages.resources.databasePort}<input className="shortNumberControl" required type="number" min={1} max={65535} value={connectDraft.port}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, port: event.target.value }))} /></label>
                  <label>{messages.resources.databaseName}<input required maxLength={253} value={connectDraft.database}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, database: event.target.value }))} /></label>
                  <label>{messages.resources.databaseUser}<input required maxLength={253} value={connectDraft.username}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, username: event.target.value }))} /></label>
                  <label>{messages.resources.databaseTls}<Select disabled={locked} value={connectDraft.sslmode}
                    onValueChange={(nextValue) => setConnectDraft((value) => ({ ...value, sslmode: nextValue }))}>
                    <option value="verify-full">{messages.resources.tlsVerifyFull}</option>
                    <option value="require">{messages.resources.tlsRequire}</option>
                    <option value="disable">{messages.resources.tlsDisable}</option>
                  </Select></label>
                </>
              ) : connectDraft.provider === 'mcp' ? (
                <label>{messages.resources.mcpServerUrl}<input type="url" required maxLength={2048}
                  placeholder="https://mcp.example.com/mcp" value={connectDraft.serverUrl}
                  onChange={(event) => setConnectDraft((value) => ({ ...value, serverUrl: event.target.value, mcpCatalog: null, mcpTools: false }))} /></label>
              ) : connectDraft.provider === 'http' ? (
                <>
                  <label>{messages.resources.baseUrlLabel}<input type="url" required value={connectDraft.baseUrl}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, baseUrl: event.target.value }))} /></label>

                </>
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
                        <Select
                  disabled={locked}
                          value={connectDraft.writeMode}
                          onValueChange={(nextValue) => setConnectDraft((value) => ({
                            ...value,
                            writeMode: nextValue as RepositoryWriteMode,
                          }))}
                        >
                          <option value="direct">{messages.resources.writeModeDirect}</option>
                          <option value="branch">{messages.resources.writeModeBranch}</option>
                        </Select>
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
              <h3 className="resourceSectionTitle">{messages.resourcesAudit.authenticationSection}</h3>
              {connectDraft.provider === 'http' && <>
                  <label>{messages.resources.httpAuthentication}<Select disabled={locked} value={connectDraft.httpAuthMode}
                    onValueChange={(nextValue) => setConnectDraft((value) => ({ ...value, httpAuthMode: nextValue as ConnectDraft['httpAuthMode'], credentialChoice: nextValue === 'none' ? '' : value.credentialChoice }))}>
                    <option value="none">{messages.resources.notUsed}</option><option value="bearer">Bearer</option><option value="header">API Key header</option>
                  </Select></label>
                  {connectDraft.httpAuthMode === 'header' && <label>{messages.resources.httpCredentialHeader}<input required value={connectDraft.httpAuthHeader}
                    placeholder="X-Redmine-API-Key" onChange={(event) => setConnectDraft((value) => ({ ...value, httpAuthHeader: event.target.value }))} /></label>}
              </>}
              <label>{messages.resources.credentialLabel}
                <Select
                  disabled={locked}
                  required={connectForm.requiresSecret || (connectDraft.provider === 'http' && connectDraft.httpAuthMode !== 'none') || (connectDraft.provider === 'mcp' && connectDraft.mcpTools && connectDraft.access === 'read_write')}
                  value={connectDraft.credentialChoice}
                  onValueChange={(nextValue) => setConnectDraft((value) => ({ ...value, credentialChoice: nextValue, secretValue: '', locator: '' }))}
                >
                  {!connectForm.requiresSecret && <option value="" disabled={connectDraft.provider === 'mcp' && connectDraft.mcpTools && connectDraft.access === 'read_write'}>{messages.resources.notUsed}</option>}
                  <option value={NEW_CREDENTIAL}>{messages.resources.credentialNew}</option>
                  {connectSecrets.map((item) => (
                    <option key={item.secret_reference_id} value={item.secret_reference_id}>{item.name}</option>
                  ))}
                </Select>
              </label>
              {selectedConnectSecret && (
                <SecretResolverFields disabled={locked} editing provider={connectDraft.provider}
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
                  <SecretResolverFields disabled={locked}
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
              <h3 className="resourceSectionTitle">{messages.resourcesAudit.scopeSection}</h3>
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
                <>
                  <p className="hint">{messages.resources.nativeSqlHint}</p>
                  {connectWriteEnabled && connectDraft.access === 'read_write' && <fieldset className="scopePicker"><legend>{messages.resources.databaseOperations}</legend>
                    {['INSERT', 'UPDATE', 'DELETE'].map((operation) => <label className="scopeOption" key={operation}>
                      <input type="checkbox" checked={connectDraft.databaseOperations.includes(operation)}
                        onChange={(event) => setConnectDraft((value) => ({ ...value, databaseOperations: event.target.checked ? [...value.databaseOperations, operation] : value.databaseOperations.filter((v) => v !== operation) }))} />
                      <span>{operation}</span>
                    </label>)}
                  </fieldset>}
                </>
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
                    <details className="resourceToolCatalog">
                      <summary>{messages.resourcesAudit.mcpToolCount(mcpToolNames(connectDraft.mcpCatalog).length)}</summary>
                      <div className="resourceToolCatalogList" tabIndex={0} role="region" aria-label={messages.resourcesAudit.mcpToolCount(mcpToolNames(connectDraft.mcpCatalog).length)}>
                    {mcpToolNames(connectDraft.mcpCatalog).map((name) => {
                      const mode = connectDraft.mcpTools ? mcpPermissionsForAccess(connectDraft.mcpCatalog, connectDraft.access)[name] : undefined
                      return <div key={name} className="resourceToolPermission">
                        <code>{name}</code>
                        <span>{mode === 'read' ? messages.resources.mcpToolRead : mode === 'call' ? messages.resources.mcpToolCall : messages.resources.mcpToolDenied}</span>
                      </div>
                    })}
                      </div>
                    </details>
                  </>}
                </div>}
                <label>{messages.resources.mcpResourceUris}<textarea className="mono compactTextarea"
                  value={connectDraft.resourceUris}
                  onChange={(event) => setConnectDraft((value) => ({ ...value, resourceUris: event.target.value }))} />
                  <span className="hint">{messages.resources.mcpReadHint}</span></label>
                </>
              ) : connectDraft.provider === 'http' ? (
                <>
                  <label>{messages.resources.httpPaths}<textarea required className="mono compactTextarea" value={connectDraft.paths}
                    onChange={(event) => setConnectDraft((value) => ({ ...value, paths: event.target.value }))} /></label>
                  {connectWriteEnabled && connectDraft.access === 'read_write' && <fieldset className="scopePicker"><legend>{messages.resources.httpMethods}</legend>
                    {['POST', 'PUT', 'PATCH', 'DELETE'].map((method) => <label className="scopeOption" key={method}>
                      <input type="checkbox" checked={connectDraft.httpMethods.includes(method)}
                        onChange={(event) => setConnectDraft((value) => ({ ...value, httpMethods: event.target.checked ? [...value.httpMethods, method] : value.httpMethods.filter((v) => v !== method) }))} />
                      <span>{method}</span>
                    </label>)}
                  </fieldset>}
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
              </fieldset>
              {error && <p className="error" role="alert">{error}</p>}
              {feedback}
              <div className="resourceFormActions resourceFormActionsSticky">
                <button className="secondaryButton" onClick={onClose} type="button">{messages.resourcesAudit.close}</button>
                <button className="primaryButton" disabled={locked} type="submit">
                  {busy === 'connect' ? messages.resourcesAudit.saving : editingIntegration === null ? messages.resources.connectSubmit : messages.resources.save}
                </button>
              </div>
            </form>
  )
}
