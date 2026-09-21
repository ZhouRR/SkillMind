import { discoverMcpTools } from '../api/integrations'
import { supportsMcpCatalog } from '../lib/resourceConfig'
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'

import {
  createEffectPreauthorization,
  createIntegration,
  loadIntegrationDetails,
  updateIntegration,
  updateSecretReference,
  deleteIntegration,
  deleteSecretReference,
  createSecretReference,
  disableEffectPreauthorization,
  disableIntegration,
  disableSecretReference,
  putResourceBinding,
  type IntegrationRecord,
  type ResourceBindingRecord,
  type SecretReferenceRecord,
} from '../api'
import { EmptyState, LoadingSkeleton, ModalDialog, PageHeader } from '../components/PageElements'
import { useMessages } from '../i18n'
import { useResourceAdministration } from '../hooks/useResourceAdministration'
import { prepareResourceConnection, resourceWriteEnabled } from '../lib/resourceConnection'
import { ResourceConnectionForm } from '../components/ResourceConnectionForm'
import { ResourceList, ResourceTabButton, SecretResolverFields, ScopeSubsetPicker } from '../components/ResourceFormFields'
import {
  PROVIDER_FORMS,
  RESOURCE_PROVIDERS,
  accessForCapabilities,
  asResourceProvider,
  collectRequirementOptions,
  findScopeIssue,
  isWriteCapability,
  requirementOptionsForTask,
  scopeDraftFromScope,
  scopeFromDraft,
  selectBindingCapability,
  summarizeScope,
  taskOptionLabel,
  taskScopeKey,
  type ResourceProvider,
  type ScopeIssue,
} from '../lib/resourceConfig'
import {
  CUSTOM_REQUIREMENT,
  EMPTY_BINDING,
  EMPTY_POLICY,
  EMPTY_SECRET,
  NEW_CREDENTIAL,
  PROVIDER_LABELS,
  emptyConnectDraft,
  connectDraftFromIntegration,
  secretInputFromDraft,
  type BindingDraft,
  type ConnectDraft,
  type PolicyDraft,
  type ResourceDialog,
  type ResourceTab,
  type SecretDraft,
} from '../lib/resourceDrafts'

/** Integration、SecretReference、ResourceBinding と effect policy を PostgreSQL で管理する。

    認証情報・接続先・権限を一つの form に束ね、個別の Secret 管理・binding・事前許可は
    高度設定として折り畳む。scope/config の裸 JSON 入力は構造化入力へ置き換え、
    検証の最終権威は server 側に置いたまま「確実に弾かれる入力」だけを送信前に知らせる。 */
export function ResourcesPage({ projectId, csrfToken, deferredFeaturesEnabled = true, databaseWritesEnabled = false, gitWritesEnabled = false, mcpToolsEnabled = false }: {
  projectId: string
  csrfToken: string
  deferredFeaturesEnabled?: boolean
  databaseWritesEnabled?: boolean
  gitWritesEnabled?: boolean
  mcpToolsEnabled?: boolean
}) {
  const messages = useMessages()
  const features = { deferredFeaturesEnabled, databaseWritesEnabled, gitWritesEnabled, mcpToolsEnabled }
  const { secrets, integrations, bindings, policies, tasks, loading, busy, error, setError,
    perform, recordSecret } = useResourceAdministration(projectId, deferredFeaturesEnabled)
  const [connectDraft, setConnectDraft] = useState<ConnectDraft>(() => emptyConnectDraft('redmine'))
  const [secretDraft, setSecretDraft] = useState<SecretDraft>(EMPTY_SECRET)
  const [bindingDraft, setBindingDraft] = useState<BindingDraft>(EMPTY_BINDING)
  const [policyDraft, setPolicyDraft] = useState<PolicyDraft>(EMPTY_POLICY)
  const [resourceTab, setResourceTab] = useState<ResourceTab>('secret')
  const [openDialog, setOpenDialog] = useState<ResourceDialog | null>(null)
  const [editingIntegration, setEditingIntegration] = useState<IntegrationRecord | null>(null)
  const [editingSecret, setEditingSecret] = useState<SecretReferenceRecord | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<IntegrationRecord | SecretReferenceRecord | null>(null)
  const savedMcpUrl = useRef('')

  useEffect(() => {
    setOpenDialog(null); setDeleteTarget(null); setEditingIntegration(null); setEditingSecret(null)
    setConnectDraft(emptyConnectDraft('redmine')); setSecretDraft(EMPTY_SECRET)
    setBindingDraft(EMPTY_BINDING); setPolicyDraft(EMPTY_POLICY)
  }, [projectId])

  // 配備状態の再取得で後置機能が閉じた場合、非表示 tab に取り残さない。
  useEffect(() => {
    if (!deferredFeaturesEnabled) {
      setResourceTab((current) => current === 'policy' ? 'secret' : current)
      setOpenDialog((current) => current === 'policy' ? null : current)
    }
    setConnectDraft((current) => resourceWriteEnabled(current.provider, features)
      ? current : { ...current, access: 'read' })
  }, [deferredFeaturesEnabled, databaseWritesEnabled, gitWritesEnabled, mcpToolsEnabled])

  /** Server 側で確実に拒否される scope の理由を利用者言語の文言へ写像する。 */
  function scopeIssueText(issue: ScopeIssue): string {
    if (issue === 'issue_ids_required') return messages.resources.issueIdsRequired
    if (issue === 'field_keys_required') return messages.resources.fieldKeysRequired
    if (issue === 'write_columns_required') return messages.resources.databaseWriteColumnsRequired
    if (issue === 'database_operations_required') return messages.resources.databaseOperationsRequired
    if (issue === 'mcp_tools_required') return messages.resources.mcpToolsRequired
    if (issue === 'tables_required') return messages.resources.tablesRequired
    return messages.resources.pathsRequired
  }

  /** 新規作成弹窗を開く。前回操作の error を持ち越さない(草稿は保持する)。 */
  function showDialog(dialog: ResourceDialog): void {
    if (busy !== null) return
    setError(null)
    setEditingIntegration(null); setEditingSecret(null)
    setConnectDraft(emptyConnectDraft(connectDraft.provider)); setSecretDraft(EMPTY_SECRET)
    setOpenDialog(dialog)
  }

  /** 最新の revision と非機密設定を読み、同じ接続 form で編集する。 */
  async function editIntegration(item: IntegrationRecord): Promise<void> {
    await perform('load-edit', async (signal) => {
      const detail = await loadIntegrationDetails(projectId, item.integration_id, signal)
      if (signal.aborted) return
      savedMcpUrl.current = typeof detail.config.server_url === 'string' ? detail.config.server_url : ''
      setEditingIntegration(detail.integration)
      setConnectDraft(connectDraftFromIntegration(detail.integration, detail.config))
      setOpenDialog('connect')
    }, 'read')
  }

  /** 保存済み接続だけを発見し、対象変更・閉鎖後の遅延結果は破棄する。 */
  async function discoverTools(): Promise<void> {
    const item = editingIntegration
    if (!item || connectDraft.serverUrl.trim() !== savedMcpUrl.current
      || connectDraft.credentialChoice !== (item.secret_reference_id ?? '') || connectDraft.secretValue || connectDraft.locator) {
      setError(messages.resources.mcpSaveFirst); return
    }
    await perform('mcp-discover', async (signal) => {
      const result = await discoverMcpTools(projectId, item.integration_id, item.revision, csrfToken, signal)
      if (signal.aborted) return
      if (!supportsMcpCatalog(result.catalog)) setError(messages.resources.mcpUnsupported)
      setConnectDraft((current) => current.provider === 'mcp' && current.serverUrl.trim() === savedMcpUrl.current
        ? { ...current, mcpCatalog: result.catalog, mcpTools: supportsMcpCatalog(result.catalog) } : current)
    }, 'read')
  }

  /** 原値を取得せず、認証情報の metadata と任意の差し替え入力を開く。 */
  function editSecret(item: SecretReferenceRecord): void {
    const provider = asResourceProvider(item.provider)
    if (provider === null) return
    setEditingSecret(item)
    setSecretDraft({ ...EMPTY_SECRET, name: item.name, provider, resolver: item.resolver, key_version: item.key_version })
    setError(null); setOpenDialog('secret')
  }

  /** 確認した対象の version で一度だけ削除する。 */
  async function removeResource(): Promise<void> {
    const item = deleteTarget
    if (item === null) return
    const done = await perform('delete', (signal) => 'integration_id' in item
      ? deleteIntegration(projectId, item.integration_id, item.revision, csrfToken, signal)
      : deleteSecretReference(projectId, item.secret_reference_id, item.updated_at, csrfToken, signal))
    if (done) setDeleteTarget(null)
  }

  /** 認証情報と Integration を一回の操作で登録し、値は専用 Secret API にだけ送る。 */
  async function submitConnect(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    const draft = connectDraft
    const prepared = prepareResourceConnection(draft, connectWriteEnabled)
    if (!prepared.valid) {
      setError('scopeIssue' in prepared ? scopeIssueText(prepared.scopeIssue)
        : messages.resources.writeConfigIssue[prepared.writeIssue])
      return
    }
    const succeeded = await perform('connect', async (signal) => {
      let secretReferenceId: string | null = null
      if (draft.credentialChoice === NEW_CREDENTIAL) {
        // 凭据は Integration と同名で登録する。どちらも project 内一意なので、
        // 名前衝突は server の 409 として利用者に見える。
        const secret = await createSecretReference(projectId, secretInputFromDraft({
          name: draft.name,
          provider: draft.provider,
          resolver: draft.resolver,
          locator: draft.locator,
          secretValue: draft.secretValue,
          keyVersion: 'v1',
        }), csrfToken, signal)
        secretReferenceId = secret.secret_reference_id
        if (signal.aborted) return
        recordSecret(secret)
        setConnectDraft((current) => ({ ...current, credentialChoice: secret.secret_reference_id, secretValue: '', locator: '' }))
      } else if (draft.credentialChoice !== '') {
        secretReferenceId = draft.credentialChoice
        const current = secrets.find((item) => item.secret_reference_id === secretReferenceId)
        if (current && (draft.secretValue !== '' || draft.locator !== '')) {
          const updated = await updateSecretReference(projectId, current.secret_reference_id, {
            name: current.name, key_version: current.key_version, expected_updated_at: current.updated_at,
            ...(draft.secretValue === '' ? {} : { secret_value: draft.secretValue }),
            ...(draft.locator === '' ? {} : { locator: draft.locator }),
          }, csrfToken, signal)
          if (signal.aborted) return
          recordSecret(updated)
          setConnectDraft((value) => ({ ...value, secretValue: '', locator: '' }))
        }
      }
      if (signal.aborted) return
      const input = { ...prepared.input, secret_reference_id: secretReferenceId }
      if (editingIntegration === null) await createIntegration(projectId, input, csrfToken, signal)
      else await updateIntegration(projectId, editingIntegration.integration_id,
        { ...input, expected_revision: editingIntegration.revision }, csrfToken, signal)
    })
    if (succeeded) {
      setEditingIntegration(null)
      setConnectDraft(emptyConnectDraft(draft.provider))
      setOpenDialog(null)
    }
  }

  /** Secret 本文を送らず resolver locator だけを事前登録する(高度設定)。 */
  async function submitSecret(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    const succeeded = await perform('secret', (signal) => editingSecret !== null
      ? updateSecretReference(projectId, editingSecret.secret_reference_id, {
        name: secretDraft.name, key_version: secretDraft.key_version,
        expected_updated_at: editingSecret.updated_at,
        ...(secretDraft.locator === '' ? {} : { locator: secretDraft.locator }),
        ...(secretDraft.secret_value === '' ? {} : { secret_value: secretDraft.secret_value }),
      }, csrfToken, signal)
      : createSecretReference(
      projectId,
      secretInputFromDraft({
        name: secretDraft.name,
        provider: secretDraft.provider,
        resolver: secretDraft.resolver,
        locator: secretDraft.locator,
        secretValue: secretDraft.secret_value,
        keyVersion: secretDraft.key_version,
      }),
      csrfToken,
      signal,
    ))
    if (succeeded) {
      setSecretDraft(EMPTY_SECRET)
      setEditingSecret(null)
      setOpenDialog(null)
    }
  }

  // ---- Binding form の導出値。requirement は公開 task の要求から選ぶ。 ----
  const activeIntegrations = integrations.filter((item) => item.status === 'ACTIVE')
  const selectedBindingTask = tasks.find((task) => taskScopeKey(task) === bindingDraft.taskKey)
  const requirementOptions = useMemo(() => (
    bindingDraft.scope_level === 'TASK'
      ? (selectedBindingTask === undefined ? [] : requirementOptionsForTask(selectedBindingTask))
      : collectRequirementOptions(tasks)
  ), [tasks, bindingDraft.scope_level, selectedBindingTask])
  const useCustomRequirement = requirementOptions.length === 0
    || bindingDraft.requirementChoice === CUSTOM_REQUIREMENT
  const selectedRequirement = useCustomRequirement
    ? undefined
    : requirementOptions.find((option) => option.key === bindingDraft.requirementChoice)
  // Requirement の kind は Run 解決時の硬境界なので、候補の時点で合わない Integration を出さない。
  const bindingIntegrationOptions = selectedRequirement !== undefined && selectedRequirement.kind !== ''
    ? activeIntegrations.filter((item) => item.kind === selectedRequirement.kind)
    : activeIntegrations
  const bindingIntegration = activeIntegrations.find(
    (item) => item.integration_id === bindingDraft.integration_id,
  )
  // Run 解決と同じ規則で capability を自動導出し、保存後に stale 拒否される組合せを防ぐ。
  const bindingCapability = bindingIntegration === undefined
    ? null
    : selectedRequirement !== undefined
      ? selectBindingCapability(bindingIntegration.capabilities, {
        hints: selectedRequirement.capabilities,
        access: selectedRequirement.access,
        observeCapability: selectedRequirement.observeCapability,
      })
      : (bindingDraft.capabilityFallback === '' ? null : bindingDraft.capabilityFallback)
  const bindingCapabilityMissing = bindingIntegration !== undefined
    && selectedRequirement !== undefined
    && bindingCapability === null

  /** Project default/Task override の binding を Integration scope の部分集合として保存する。 */
  async function submitBinding(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (bindingIntegration === undefined) {
      setError(messages.resources.selectValidIntegration)
      return
    }
    const provider = asResourceProvider(bindingIntegration.provider)
    const requirementKey = useCustomRequirement
      ? bindingDraft.requirementCustom.trim()
      : bindingDraft.requirementChoice
    if (provider === null || requirementKey === '' || bindingCapability === null) {
      setError(messages.resources.noCapabilityForRequirement)
      return
    }
    const requestedScope = scopeFromDraft(bindingDraft.scopeDraft)
    const issue = findScopeIssue(provider, requestedScope, isWriteCapability(bindingCapability))
    if (issue !== null) {
      setError(scopeIssueText(issue))
      return
    }
    const succeeded = await perform('binding', (signal) => putResourceBinding(projectId, {
      scope_level: bindingDraft.scope_level,
      scope_key: bindingDraft.scope_level === 'PROJECT_DEFAULT' ? 'project' : bindingDraft.taskKey.trim(),
      requirement_key: requirementKey,
      resource_kind: selectedRequirement !== undefined && selectedRequirement.kind !== ''
        ? selectedRequirement.kind
        : bindingIntegration.kind,
      integration_id: bindingIntegration.integration_id,
      capability_version: bindingCapability,
      requested_scope: requestedScope,
    }, csrfToken, signal))
    if (succeeded) {
      setBindingDraft(EMPTY_BINDING)
      setOpenDialog(null)
    }
  }

  // ---- 事前許可 form の導出値。現行 apply gate は Redmine issue.update/v1 のみ。 ----
  const writableIntegrations = activeIntegrations.filter(
    (item) => item.provider === 'redmine' && item.capabilities.some(isWriteCapability),
  )
  const policyIntegration = writableIntegrations.find(
    (item) => item.integration_id === policyDraft.integration_id,
  )

  /** Redmine issue.update/v1 の LOW-only exact preauthorization を登録する。 */
  async function submitPolicy(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (policyIntegration === undefined) {
      setError(messages.resources.selectValidIntegration)
      return
    }
    const scope = scopeFromDraft(policyDraft.scopeDraft)
    const issue = findScopeIssue('redmine', scope, true)
    if (issue !== null) {
      setError(scopeIssueText(issue))
      return
    }
    const succeeded = await perform('policy', (signal) => createEffectPreauthorization(projectId, {
      integration_id: policyDraft.integration_id,
      capability_version: PROVIDER_FORMS.redmine.writeCapability ?? '',
      operation: policyDraft.operation,
      risk_level: 'LOW',
      scope,
      expires_at: policyDraft.expires_at
        ? new Date(policyDraft.expires_at).toISOString()
        : null,
    }, csrfToken, signal))
    if (succeeded) {
      setPolicyDraft(EMPTY_POLICY)
      setOpenDialog(null)
    }
  }

  const connectWriteEnabled = resourceWriteEnabled(connectDraft.provider, features)
    && (connectDraft.provider !== 'mcp' || connectDraft.mcpTools)
  const bindingLevelText = (level: ResourceBindingRecord['scope_level']): string => (
    level === 'PROJECT_DEFAULT'
      ? messages.resources.projectDefault
      : level === 'TASK' ? messages.resources.taskOverride : 'Run'
  )
  return (
    <>
      <PageHeader
        title={messages.resources.title}
        aside={<span className="scopeBadge">{messages.resources.scopeBadge}</span>}
      />
      {!projectId && <EmptyState text={messages.resources.selectProjectFirst} />}
      {projectId && (
        <div className="resourceAdmin">
          {/* 弹窗が開いている間の error は弹窗内に出す。ここは一覧上の操作(停用など)の失敗用。 */}
          {error && openDialog === null && deleteTarget === null && <p className="error resourceAdminError" role="alert">{error}</p>}
          {loading && <LoadingSkeleton label={messages.resources.loadingConfig} rows={2} />}
          {/* 通常操作は認証情報と権限の一覧・追加だけで完結する。 */}
          <section className="resourceTabPanel" aria-label={messages.resources.integrationListTitle}>
            <section className="panel">
              <div className="panelHeader">
                <h2>{messages.resources.integrationListTitle}</h2>
                <div className="panelHeaderActions">
                  <span className="eventCount">{integrations.length}</span>
                  <button className="secondaryButton" type="button" onClick={() => showDialog('connect')}>
                    {messages.resources.connectTitle}
                  </button>
                </div>
              </div>
              <ResourceList busy={busy !== null} emptyText={messages.resources.connectGuide} items={integrations.map((item) => {
                const provider = asResourceProvider(item.provider)
                const accessText = accessForCapabilities(item.capabilities) === 'read_write'
                  ? messages.resources.accessBadgeReadWrite
                  : messages.resources.accessBadgeRead
                return {
                  id: item.integration_id,
                  title: item.name,
                  detail: `${provider === null ? item.provider : PROVIDER_LABELS[provider]} · ${accessText} · ${summarizeScope(item.scope, { wildcardLabel: messages.resources.scopeUnrestrictedLabel })}`,
                  status: item.status,
                  updatedAt: item.updated_at,
                  onEdit: () => void editIntegration(item),
                  onDelete: () => { setError(null); setDeleteTarget(item) },
                  onDisable: item.status === 'ACTIVE' ? () => void perform(`integration-${item.integration_id}`, (signal) => disableIntegration(projectId, item.integration_id, item.revision, csrfToken, signal)) : undefined,
                }
              })} />
            </section>
          </section>

          <ModalDialog open={deleteTarget !== null} title={messages.resources.deleteConfirm}
            onClose={() => { if (busy === null) setDeleteTarget(null) }}>
            <p>{deleteTarget?.name}</p>
            {error && deleteTarget !== null && <p className="error" role="alert">{error}</p>}
            <div className="panelHeaderActions">
              <button className="secondaryButton" disabled={busy !== null} onClick={() => setDeleteTarget(null)} type="button">{messages.resources.cancel}</button>
              <button className="dangerButton" disabled={busy !== null} onClick={() => void removeResource()} type="button">{messages.resources.delete}</button>
            </div>
          </ModalDialog>
          <ModalDialog
            open={openDialog === 'connect'}
            drawer
            title={editingIntegration === null ? messages.resources.connectTitle : messages.resources.edit}
            wide
            onClose={() => { if (busy === null) setOpenDialog(null) }}
          >
            <ResourceConnectionForm connectDraft={connectDraft} setConnectDraft={setConnectDraft}
              secrets={secrets} editingIntegration={editingIntegration} connectWriteEnabled={connectWriteEnabled}
              mcpToolsEnabled={mcpToolsEnabled} busy={busy} error={openDialog === 'connect' ? error : null}
              submitConnect={submitConnect} discoverTools={discoverTools} />
          </ModalDialog>

          <details className="detailDisclosure resourceAdvanced">
            <summary>{messages.resources.advancedTitle}</summary>
            <p className="hint">{messages.resources.advancedHint}</p>
            <div className="tabBar" role="tablist" aria-label={messages.resources.tabsAria}>
              <ResourceTabButton current={resourceTab} tab="secret" onSelect={setResourceTab}>{messages.resources.tabSecret}</ResourceTabButton>
              <ResourceTabButton current={resourceTab} tab="binding" onSelect={setResourceTab}>{messages.resources.tabBinding}</ResourceTabButton>
              {deferredFeaturesEnabled && <ResourceTabButton current={resourceTab} tab="policy" onSelect={setResourceTab}>{messages.resources.tabPolicy}</ResourceTabButton>}
            </div>
            <section className="resourceTabPanel tabPanel" role="tabpanel" hidden={resourceTab !== 'secret'}>
              <section className="panel">
                <div className="panelHeader">
                  <h2>{messages.resources.secretTitle}</h2>
                  <div className="panelHeaderActions">
                    <span className="eventCount">{secrets.length}</span>
                    <button className="secondaryButton" type="button" onClick={() => showDialog('secret')}>
                      {messages.resources.registerLocator}
                    </button>
                  </div>
                </div>
                <p className="hint">{messages.resources.secretAdvancedHint}</p>
                <ResourceList busy={busy !== null} items={secrets.map((item) => ({
                  id: item.secret_reference_id,
                  title: item.name,
                  detail: `${item.provider} · ${item.resolver} · ${item.key_version}`,
                  status: item.status,
                  updatedAt: item.updated_at,
                  onEdit: () => editSecret(item),
                  onDelete: () => { setError(null); setDeleteTarget(item) },
                  onDisable: item.status === 'ACTIVE' ? () => void perform(`secret-${item.secret_reference_id}`, (signal) => disableSecretReference(projectId, item.secret_reference_id, csrfToken, signal)) : undefined,
                }))} />
              </section>
            </section>

            <ModalDialog
              open={openDialog === 'secret'}
              drawer
              title={editingSecret === null ? messages.resources.registerLocator : messages.resources.edit}
              onClose={() => { if (busy === null) setOpenDialog(null) }}
            >
              <form className="resourceForm" onSubmit={(event) => void submitSecret(event)}>
                  <label>{messages.resources.nameLabel}<input required value={secretDraft.name} onChange={(event) => setSecretDraft((value) => ({ ...value, name: event.target.value }))} /></label>
                  <label>{messages.resources.providerLabel}
                    <select disabled={editingSecret !== null} value={secretDraft.provider} onChange={(event) => setSecretDraft((value) => ({ ...value, provider: event.target.value as ResourceProvider, secret_value: '', locator: '' }))}>
                      {RESOURCE_PROVIDERS.map((provider) => (
                        <option key={provider} value={provider}>{PROVIDER_LABELS[provider]}</option>
                      ))}
                    </select>
                  </label>
                  <SecretResolverFields
                    editing={editingSecret !== null}
                    provider={secretDraft.provider}
                    resolver={secretDraft.resolver}
                    locator={secretDraft.locator}
                    secretValue={secretDraft.secret_value}
                    envExample={PROVIDER_FORMS[secretDraft.provider].environmentLocatorExample}
                    fileExample={PROVIDER_FORMS[secretDraft.provider].fileLocatorExample}
                    onResolver={(resolver) => setSecretDraft((value) => ({ ...value, resolver }))}
                    onLocator={(locator) => setSecretDraft((value) => ({ ...value, locator }))}
                    onSecretValue={(secretValue) => setSecretDraft((value) => ({ ...value, secret_value: secretValue }))}
                  />
                  <label>{messages.resources.keyVersionLabel}<input value={secretDraft.key_version} onChange={(event) => setSecretDraft((value) => ({ ...value, key_version: event.target.value }))} /></label>
                  <p className="hint resourceWarning">{secretDraft.resolver === 'MANAGED' ? messages.resources.secretValueHint : messages.resources.secretHint}</p>
                  {error && openDialog === 'secret' && <p className="error" role="alert">{error}</p>}
                  <button className="primaryButton" disabled={busy !== null} type="submit">{editingSecret === null ? messages.resources.registerLocator : messages.resources.save}</button>
              </form>
            </ModalDialog>

            <section className="resourceTabPanel tabPanel" role="tabpanel" hidden={resourceTab !== 'binding'}>
              <section className="panel">
                <div className="panelHeader">
                  <h2>{messages.resources.bindingTitle}</h2>
                  <div className="panelHeaderActions">
                    <span className="eventCount">{bindings.length}</span>
                    <button className="secondaryButton" type="button" onClick={() => showDialog('binding')}>
                      {messages.resources.newBinding}
                    </button>
                  </div>
                </div>
                <p className="hint">{messages.resources.bindingHint}</p>
                <ResourceList busy={busy !== null} items={bindings.map((item) => ({
                  id: item.binding_id,
                  title: `${item.requirement_key} · ${bindingLevelText(item.scope_level)}`,
                  detail: `${item.provider} ${item.capability_version} · ${summarizeScope(item.scope, { wildcardLabel: messages.resources.scopeUnrestrictedLabel })}`,
                  status: item.run_id ? 'FROZEN' : 'ACTIVE',
                  updatedAt: item.updated_at,
                }))} />
              </section>
            </section>

            <ModalDialog
              open={openDialog === 'binding'}
              drawer
              title={messages.resources.bindingTitle}
              wide
              onClose={() => { if (busy === null) setOpenDialog(null) }}
            >
              <form className="resourceForm" onSubmit={(event) => void submitBinding(event)}>
                  <p className="hint">{messages.resources.bindingHint}</p>
                  <label>{messages.resources.levelLabel}
                    <select
                      value={bindingDraft.scope_level}
                      onChange={(event) => setBindingDraft((value) => ({
                        ...value,
                        scope_level: event.target.value as BindingDraft['scope_level'],
                        taskKey: '',
                        requirementChoice: '',
                      }))}
                    >
                      <option value="PROJECT_DEFAULT">{messages.resources.projectDefault}</option>
                      <option value="TASK">{messages.resources.taskOverride}</option>
                    </select>
                  </label>
                  {bindingDraft.scope_level === 'TASK' && (tasks.length > 0 ? (
                    <label>{messages.resources.taskSelectLabel}
                      <select
                        required
                        value={bindingDraft.taskKey}
                        onChange={(event) => setBindingDraft((value) => ({ ...value, taskKey: event.target.value, requirementChoice: '' }))}
                      >
                        <option value="">{messages.resources.pleaseSelect}</option>
                        {tasks.map((task) => (
                          <option key={taskScopeKey(task)} value={taskScopeKey(task)}>{taskOptionLabel(task)}</option>
                        ))}
                      </select>
                    </label>
                  ) : (
                    <label>{messages.resources.taskScopeKeyLabel}
                      <input
                        className="mono"
                        required
                        value={bindingDraft.taskKey}
                        onChange={(event) => setBindingDraft((value) => ({ ...value, taskKey: event.target.value }))}
                      />
                    </label>
                  ))}
                  {requirementOptions.length > 0 && (
                    <label>{messages.resources.requirementKeyLabel}
                      <select
                        required
                        value={bindingDraft.requirementChoice}
                        onChange={(event) => {
                          const choice = event.target.value
                          const option = requirementOptions.find((item) => item.key === choice)
                          setBindingDraft((value) => {
                            // Requirement の kind と合わない Integration 選択は残さない。
                            const keepIntegration = option === undefined
                              || option.kind === ''
                              || activeIntegrations.some((item) => item.integration_id === value.integration_id && item.kind === option.kind)
                            return {
                              ...value,
                              requirementChoice: choice,
                              integration_id: keepIntegration ? value.integration_id : '',
                              scopeDraft: keepIntegration ? value.scopeDraft : [],
                            }
                          })
                        }}
                      >
                        <option value="">{messages.resources.pleaseSelect}</option>
                        {requirementOptions.map((option) => (
                          <option key={option.key} value={option.key}>
                            {option.key} · {option.taskLabels.join(' / ')}
                          </option>
                        ))}
                        <option value={CUSTOM_REQUIREMENT}>{messages.resources.requirementCustomOption}</option>
                      </select>
                    </label>
                  )}
                  {useCustomRequirement && (
                    <label>{messages.resources.requirementKeyInputLabel}
                      <input
                        className="mono"
                        pattern="[a-z][a-z0-9_.\-]*"
                        required
                        value={bindingDraft.requirementCustom}
                        onChange={(event) => setBindingDraft((value) => ({ ...value, requirementCustom: event.target.value }))}
                      />
                    </label>
                  )}
                  <label>{messages.resources.integrationSelectLabel}
                    <select
                      required
                      value={bindingDraft.integration_id}
                      onChange={(event) => {
                        const integration = activeIntegrations.find((item) => item.integration_id === event.target.value)
                        setBindingDraft((value) => ({
                          ...value,
                          integration_id: event.target.value,
                          capabilityFallback: '',
                          scopeDraft: integration === undefined
                            ? []
                            : scopeDraftFromScope(integration.scope, { keepAll: true }),
                        }))
                      }}
                    >
                      <option value="">{messages.resources.pleaseSelect}</option>
                      {bindingIntegrationOptions.map((item) => (
                        <option key={item.integration_id} value={item.integration_id}>{item.name} · {item.provider}</option>
                      ))}
                    </select>
                  </label>
                  {bindingIntegration !== undefined && selectedRequirement !== undefined && (
                    <p className="hint">
                      {messages.resources.derivedCapabilityLabel}: <code>{bindingCapability ?? '—'}</code>
                    </p>
                  )}
                  {bindingCapabilityMissing && (
                    <p className="error" role="alert">{messages.resources.noCapabilityForRequirement}</p>
                  )}
                  {bindingIntegration !== undefined && useCustomRequirement && (
                    <label>{messages.resources.capabilitySelectLabel}
                      <select
                        required
                        value={bindingDraft.capabilityFallback}
                        onChange={(event) => setBindingDraft((value) => ({ ...value, capabilityFallback: event.target.value }))}
                      >
                        <option value="">{messages.resources.pleaseSelect}</option>
                        {bindingIntegration.capabilities.map((capability) => (
                          <option key={capability} value={capability}>{capability}</option>
                        ))}
                      </select>
                    </label>
                  )}
                  {bindingIntegration !== undefined && (
                    <ScopeSubsetPicker
                      allowWildcard
                      entries={bindingDraft.scopeDraft}
                      legend={messages.resources.scopeSubsetLabel}
                      onChange={(next) => setBindingDraft((value) => ({ ...value, scopeDraft: next }))}
                    />
                  )}
                  {error && openDialog === 'binding' && <p className="error" role="alert">{error}</p>}
                  <button
                    className="primaryButton"
                    disabled={busy !== null || bindingCapabilityMissing}
                    type="submit"
                  >
                    {messages.resources.saveBinding}
                  </button>
              </form>
            </ModalDialog>

            <section className="resourceTabPanel tabPanel" role="tabpanel" hidden={!deferredFeaturesEnabled || resourceTab !== 'policy'}>
              <section className="panel">
                <div className="panelHeader">
                  <h2>{messages.resources.policyTitle}</h2>
                  <div className="panelHeaderActions">
                    <span className="eventCount">{policies.length}</span>
                    <button
                      className="secondaryButton"
                      disabled={writableIntegrations.length === 0}
                      type="button"
                      onClick={() => showDialog('policy')}
                    >
                      {messages.resources.newPolicy}
                    </button>
                  </div>
                </div>
                <p className="hint resourceWarning">{messages.resources.policyHint}</p>
                {writableIntegrations.length === 0 && (
                  <p className="hint">{messages.resources.noWritableIntegration}</p>
                )}
                <ResourceList busy={busy !== null} items={policies.map((item) => ({
                  id: item.preauthorization_id,
                  title: `${item.capability_version} · ${item.operation}`,
                  detail: `LOW · v${item.policy_version} · ${summarizeScope(item.scope, { wildcardLabel: messages.resources.scopeUnrestrictedLabel })}`,
                  status: item.status,
                  updatedAt: item.updated_at,
                  onDisable: item.status === 'ACTIVE' ? () => void perform(`policy-${item.preauthorization_id}`, (signal) => disableEffectPreauthorization(projectId, item.preauthorization_id, item.policy_version, csrfToken, signal)) : undefined,
                }))} />
              </section>
            </section>

            <ModalDialog
              open={deferredFeaturesEnabled && openDialog === 'policy'}
              drawer
              title={messages.resources.policyTitle}
              wide
              onClose={() => { if (busy === null) setOpenDialog(null) }}
            >
              <form className="resourceForm" onSubmit={(event) => void submitPolicy(event)}>
                  <p className="hint resourceWarning">{messages.resources.policyHint}</p>
                  {writableIntegrations.length > 0 && (
                    <>
                      <label>{messages.resources.integrationSelectLabel}
                        <select
                          required
                          value={policyDraft.integration_id}
                          onChange={(event) => {
                            const integration = writableIntegrations.find((item) => item.integration_id === event.target.value)
                            setPolicyDraft((value) => ({
                              ...value,
                              integration_id: event.target.value,
                              // 事前許可は explicit 必須のため、wildcard を維持する既定を与えない。
                              scopeDraft: integration === undefined
                                ? []
                                : scopeDraftFromScope(integration.scope, { keepAll: false }),
                            }))
                          }}
                        >
                          <option value="">{messages.resources.pleaseSelect}</option>
                          {writableIntegrations.map((item) => (
                            <option key={item.integration_id} value={item.integration_id}>{item.name}</option>
                          ))}
                        </select>
                      </label>
                      <label>{messages.resources.operationLabel}
                        <input
                          className="mono"
                          required
                          value={policyDraft.operation}
                          onChange={(event) => setPolicyDraft((value) => ({ ...value, operation: event.target.value }))}
                        />
                      </label>
                      <p className="hint">{messages.resources.operationHint}</p>
                      {policyIntegration !== undefined && (
                        <ScopeSubsetPicker
                          allowWildcard={false}
                          entries={policyDraft.scopeDraft}
                          legend={messages.resources.scopeSubsetLabel}
                          onChange={(next) => setPolicyDraft((value) => ({ ...value, scopeDraft: next }))}
                        />
                      )}
                      <label>{messages.resources.expiresLabel}
                        <input
                          type="datetime-local"
                          value={policyDraft.expires_at}
                          onChange={(event) => setPolicyDraft((value) => ({ ...value, expires_at: event.target.value }))}
                        />
                      </label>
                      {error && openDialog === 'policy' && <p className="error" role="alert">{error}</p>}
                      <button className="primaryButton" disabled={busy !== null} type="submit">{messages.resources.createPolicy}</button>
                    </>
                  )}
              </form>
            </ModalDialog>
          </details>
        </div>
      )}
    </>
  )
}
