import { discoverMcpTools } from '../api/integrations'
import { supportsMcpCatalog, mcpToolNames, mcpPermissionsForAccess } from '../lib/resourceConfig'
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'

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
  loadEffectPreauthorizations,
  loadIntegrations,
  loadProjectTasks,
  loadResourceBindings,
  loadSecretReferences,
  putResourceBinding,
  type EffectPreauthorizationRecord,
  type IntegrationRecord,
  type PublishedTaskRecord,
  type ResourceBindingRecord,
  type SecretReferenceRecord,
  type SecretResolver,
} from '../api'
import { EmptyState, LoadingSkeleton, ModalDialog, PageHeader } from '../components/PageElements'
import { useMessages } from '../i18n'
import { apiErrorMessage } from '../lib/apiFeedback'
import { formatLocalTimestamp } from '../lib/presentation'
import {
  COMMON_REDMINE_FIELD_KEYS,
  PROVIDER_FORMS,
  RESOURCE_PROVIDERS,
  SCOPE_WILDCARD,
  accessForCapabilities,
  asResourceProvider,
  buildIntegrationConfig,
  buildIntegrationScope,
  capabilitiesForAccess,
  collectRequirementOptions,
  findScopeIssue,
  findWriteConfigIssue,
  isWriteCapability,
  parseListInput,
  requirementOptionsForTask,
  scopeDraftFromScope,
  scopeFromDraft,
  selectBindingCapability,
  summarizeScope,
  taskOptionLabel,
  taskScopeKey,
  type RepositoryWriteMode,
  type ResourceAccess,
  type ResourceProvider,
  type ScopeDraftEntry,
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
  const [secrets, setSecrets] = useState<SecretReferenceRecord[]>([])
  const [integrations, setIntegrations] = useState<IntegrationRecord[]>([])
  const [bindings, setBindings] = useState<ResourceBindingRecord[]>([])
  const [policies, setPolicies] = useState<EffectPreauthorizationRecord[]>([])
  const [tasks, setTasks] = useState<PublishedTaskRecord[]>([])
  const [connectDraft, setConnectDraft] = useState<ConnectDraft>(() => emptyConnectDraft('redmine'))
  const [secretDraft, setSecretDraft] = useState<SecretDraft>(EMPTY_SECRET)
  const [bindingDraft, setBindingDraft] = useState<BindingDraft>(EMPTY_BINDING)
  const [policyDraft, setPolicyDraft] = useState<PolicyDraft>(EMPTY_POLICY)
  const [resourceTab, setResourceTab] = useState<ResourceTab>('secret')
  const [openDialog, setOpenDialog] = useState<ResourceDialog | null>(null)
  const [editingIntegration, setEditingIntegration] = useState<IntegrationRecord | null>(null)
  const [editingSecret, setEditingSecret] = useState<SecretReferenceRecord | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<IntegrationRecord | SecretReferenceRecord | null>(null)
  const [revision, setRevision] = useState(0)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const loadController = useRef<AbortController | null>(null)
  const mutationController = useRef<AbortController | null>(null)
  const savedMcpUrl = useRef('')

  useEffect(() => {
    mutationController.current?.abort()
    setOpenDialog(null); setDeleteTarget(null); setEditingIntegration(null); setEditingSecret(null)
    setConnectDraft(emptyConnectDraft('redmine')); setSecretDraft(EMPTY_SECRET); setBusy(null)
  }, [projectId])

  // 配備状態の再取得で後置機能が閉じた場合、非表示 tab に取り残さない。
  useEffect(() => {
    if (!deferredFeaturesEnabled) {
      setResourceTab((current) => current === 'policy' ? 'secret' : current)
      setOpenDialog((current) => current === 'policy' ? null : current)
    }
    setConnectDraft((current) => (current.provider === 'mcp' ? mcpToolsEnabled : current.provider === 'postgres' ? databaseWritesEnabled : current.provider === 'git' ? gitWritesEnabled || deferredFeaturesEnabled : deferredFeaturesEnabled)
      ? current : { ...current, access: 'read' })
  }, [deferredFeaturesEnabled, databaseWritesEnabled, gitWritesEnabled, mcpToolsEnabled])

  useEffect(() => () => {
    loadController.current?.abort()
    mutationController.current?.abort()
  }, [])

  // Project 切替時は四つの管理 projection を同じ revision で読み、混在表示を防ぐ。
  useEffect(() => {
    loadController.current?.abort()
    if (!projectId) {
      setSecrets([]); setIntegrations([]); setBindings([]); setPolicies([]); setTasks([])
      return
    }
    const controller = new AbortController()
    loadController.current = controller
    setLoading(true)
    setError(null)
    void Promise.all([
      loadSecretReferences(projectId, controller.signal),
      loadIntegrations(projectId, controller.signal),
      loadResourceBindings(projectId, controller.signal),
      deferredFeaturesEnabled ? loadEffectPreauthorizations(projectId, controller.signal) : Promise.resolve([]),
      // Task catalog は binding form の下拉候補という補助情報のため、
      // 取得失敗で管理画面全体を失敗させず自由入力へ退化させる。
      loadProjectTasks(projectId, controller.signal)
        .then((catalog) => catalog.tasks)
        .catch(() => [] as PublishedTaskRecord[]),
    ]).then(([nextSecrets, nextIntegrations, nextBindings, nextPolicies, nextTasks]) => {
      if (controller.signal.aborted) return
      setSecrets(nextSecrets)
      setIntegrations(nextIntegrations)
      setBindings(nextBindings)
      setPolicies(nextPolicies)
      setTasks(nextTasks)
    }).catch((caught: unknown) => {
      if (!controller.signal.aborted) {
        setError(apiErrorMessage(caught, messages.resources.loadFailed, messages))
      }
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [projectId, revision, deferredFeaturesEnabled])

  /** 一つの mutation を直列化し、成功時に全 projection を読み直す。 */
  async function mutate(
    key: string,
    action: (signal: AbortSignal) => Promise<unknown>,
  ): Promise<boolean> {
    mutationController.current?.abort()
    const controller = new AbortController()
    mutationController.current = controller
    setBusy(key)
    setError(null)
    try {
      await action(controller.signal)
      if (controller.signal.aborted) return false
      setRevision((current) => current + 1)
      return true
    } catch (caught: unknown) {
      if (!controller.signal.aborted) {
        setError(apiErrorMessage(caught, messages.resources.mutationFailed, messages))
      }
      return false
    } finally {
      if (!controller.signal.aborted) setBusy(null)
    }
  }

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
    await mutate('load-edit', async (signal) => {
      const detail = await loadIntegrationDetails(projectId, item.integration_id, signal)
      if (signal.aborted) return
      savedMcpUrl.current = typeof detail.config.server_url === 'string' ? detail.config.server_url : ''
      setEditingIntegration(detail.integration)
      setConnectDraft(connectDraftFromIntegration(detail.integration, detail.config))
      setOpenDialog('connect')
    })
  }

  /** 保存済み接続だけを発見し、対象変更・閉鎖後の遅延結果は破棄する。 */
  async function discoverTools(): Promise<void> {
    const item = editingIntegration
    if (!item || connectDraft.serverUrl.trim() !== savedMcpUrl.current
      || connectDraft.credentialChoice !== (item.secret_reference_id ?? '') || connectDraft.secretValue || connectDraft.locator) {
      setError(messages.resources.mcpSaveFirst); return
    }
    await mutate('mcp-discover', async (signal) => {
      const result = await discoverMcpTools(projectId, item.integration_id, item.revision, csrfToken, signal)
      if (signal.aborted) return
      if (!supportsMcpCatalog(result.catalog)) setError(messages.resources.mcpUnsupported)
      setConnectDraft((current) => current.provider === 'mcp' && current.serverUrl.trim() === savedMcpUrl.current
        ? { ...current, mcpCatalog: result.catalog, mcpTools: supportsMcpCatalog(result.catalog) } : current)
    })
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
    const done = await mutate('delete', (signal) => 'integration_id' in item
      ? deleteIntegration(projectId, item.integration_id, item.revision, csrfToken, signal)
      : deleteSecretReference(projectId, item.secret_reference_id, item.updated_at, csrfToken, signal))
    if (done) setDeleteTarget(null)
  }

  /** 認証情報と Integration を一回の操作で登録し、値は専用 Secret API にだけ送る。 */
  async function submitConnect(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    const draft = connectDraft
    const form = PROVIDER_FORMS[draft.provider]
    const access: ResourceAccess = !connectWriteEnabled || form.writeCapability === null ? 'read' : draft.access
    // 既定は明示 wildcard(不限)。field は「変更できる集合」を write 時だけ列挙でき、
    // 読取だけの integration は全 field 読取(承認境界は issue 側)とする。
    const mcpPermissions = mcpPermissionsForAccess(draft.mcpCatalog, access)
    const scope = buildIntegrationScope(draft.provider, {
      issueIds: draft.issueScope === 'all'
        ? [SCOPE_WILDCARD]
        : parseListInput(draft.issueIds),
      fieldKeys: access === 'read_write' && draft.fieldScope === 'list'
        ? [...new Set([...draft.fieldKeys, ...parseListInput(draft.customFieldKeys)])]
        : [SCOPE_WILDCARD],
      paths: parseListInput(draft.paths),
      revisions: parseListInput(draft.revisions),
      tables: parseListInput(draft.tables),
      writeEnabled: access === 'read_write',
      writeColumns: parseListInput(draft.writeColumns),
      operations: draft.databaseOperations,
      resourceUris: parseListInput(draft.resourceUris),
      mcpTools: draft.mcpTools,
      mcpPermissions,
    })
    const issue = findScopeIssue(draft.provider, scope, access === 'read_write')
    if (issue !== null) {
      setError(scopeIssueText(issue))
      return
    }
    const writeInput = {
      writeEnabled: access === 'read_write',
      writeMode: draft.writeMode,
      writeBranchPrefix: draft.writeBranchPrefix,
      forgeKind: '' as const,
      forgeApiBaseUrl: '',
      forgeProject: '',
    }
    const writeIssue = findWriteConfigIssue(draft.provider, {
      defaultRevision: draft.defaultRevision,
      write: writeInput,
    })
    if (writeIssue !== null) {
      setError(messages.resources.writeConfigIssue[writeIssue])
      return
    }
    const succeeded = await mutate('connect', async (signal) => {
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
        setSecrets((items) => [...items, secret])
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
          setSecrets((items) => items.map((item) => item.secret_reference_id === updated.secret_reference_id ? updated : item))
          setConnectDraft((value) => ({ ...value, secretValue: '', locator: '' }))
        }
      }
      if (signal.aborted) return
      const input = {
        name: draft.name,
        kind: form.kind,
        provider: draft.provider,
        capabilities: capabilitiesForAccess(draft.provider, access, draft.mcpTools, parseListInput(draft.resourceUris).length > 0),
        scope,
        config: buildIntegrationConfig(draft.provider, { ...draft, mcpPermissions, write: writeInput }),
        secret_reference_id: secretReferenceId,
      }
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
    const succeeded = await mutate('secret', (signal) => editingSecret !== null
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
    const succeeded = await mutate('binding', (signal) => putResourceBinding(projectId, {
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
    const succeeded = await mutate('policy', (signal) => createEffectPreauthorization(projectId, {
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

  const connectWriteEnabled = connectDraft.provider === 'mcp' ? mcpToolsEnabled && connectDraft.mcpTools : connectDraft.provider === 'postgres' ? databaseWritesEnabled : connectDraft.provider === 'git' ? gitWritesEnabled || deferredFeaturesEnabled : deferredFeaturesEnabled
  const connectForm = PROVIDER_FORMS[connectDraft.provider]
  const connectSecrets = secrets.filter(
    (item) => item.status === 'ACTIVE' && item.provider === connectDraft.provider,
  )
  const selectedConnectSecret = secrets.find((item) => item.secret_reference_id === connectDraft.credentialChoice)
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
                  onDisable: item.status === 'ACTIVE' ? () => void mutate(`integration-${item.integration_id}`, (signal) => disableIntegration(projectId, item.integration_id, item.revision, csrfToken, signal)) : undefined,
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
              {error && openDialog === 'connect' && <p className="error" role="alert">{error}</p>}
              <button className="primaryButton" disabled={busy !== null} type="submit">
                {editingIntegration === null ? messages.resources.connectSubmit : messages.resources.save}
              </button>
            </form>
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
                  onDisable: item.status === 'ACTIVE' ? () => void mutate(`secret-${item.secret_reference_id}`, (signal) => disableSecretReference(projectId, item.secret_reference_id, csrfToken, signal)) : undefined,
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
                  onDisable: item.status === 'ACTIVE' ? () => void mutate(`policy-${item.preauthorization_id}`, (signal) => disableEffectPreauthorization(projectId, item.preauthorization_id, item.policy_version, csrfToken, signal)) : undefined,
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

/** 資源設定タブの button。選択状態を aria-selected で表し、tablist 内で切り替える。 */
function ResourceTabButton({ current, tab, onSelect, children }: {
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
function SecretResolverFields({
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
function ResourceList({ items, emptyText, busy }: { busy: boolean; emptyText?: string; items: Array<{
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
            {item.onDisable && <button disabled={busy} className="secondaryButton" onClick={item.onDisable} type="button">{messages.resources.disable}</button>}
            {item.onDelete && <button disabled={busy} className="dangerButton" onClick={item.onDelete} type="button">{messages.resources.delete}</button>}
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
function ScopeSubsetPicker({ allowWildcard, entries, legend, onChange }: {
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
