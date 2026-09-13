import {
  API_BASE,
  hasStrings,
  isRecord,
  isStringArray,
  parseItemList,
  requestApiJson,
  requestApiEmpty,
} from './http'

/** Worker が Secret を解決する方式。MANAGED のみ明文を一度 API へ渡し密文で保存する。 */
export type SecretResolver = 'ENVIRONMENT' | 'FILE' | 'MANAGED'

/** Locator を除外した Project-scoped SecretReference metadata。 */
export interface SecretReferenceRecord {
  secret_reference_id: string
  project_id: string
  name: string
  provider: string
  resolver: SecretResolver
  key_version: string
  status: 'ACTIVE' | 'DISABLED'
  created_by: string
  created_at: string
  updated_at: string
  disabled_at: string | null
}

/** SecretReference 登録 request。locator は ENVIRONMENT/FILE 用、secret_value は MANAGED 用で排他。 */
export interface CreateSecretReferenceInput {
  name: string
  provider: string
  resolver: SecretResolver
  /** ENVIRONMENT/FILE のみ。MANAGED では省略する。 */
  locator?: string
  key_version: string
  /** MANAGED のみ。サーバが即座に KEK 封入し、平文は保存・返却・ログ化しない。 */
  secret_value?: string
}

/** 接続設定正文と Secret locator を除外した Integration metadata。 */
export interface IntegrationRecord {
  integration_id: string
  project_id: string
  name: string
  kind: string
  provider: string
  status: 'ACTIVE' | 'DISABLED'
  revision: number
  capabilities: string[]
  scope: Record<string, unknown>
  config_keys: string[]
  secret_reference_id: string | null
  created_by: string
  created_at: string
  updated_at: string
  disabled_at: string | null
}

/** 登録済み Provider instance を作成する request。 */
export interface CreateIntegrationInput {
  name: string
  kind: string
  provider: string
  capabilities: string[]
  scope: Record<string, unknown>
  config: Record<string, unknown>
  secret_reference_id: string | null
}

/** Project default、Task override または immutable Run snapshot の binding。 */
export interface ResourceBindingRecord {
  binding_id: string
  project_id: string
  scope_level: 'PROJECT_DEFAULT' | 'TASK' | 'RUN'
  scope_key: string
  requirement_key: string
  resource_kind: string
  integration_id: string | null
  run_id: string | null
  source_binding_id: string | null
  provider: string
  capability_version: string
  revision: string
  scope: Record<string, unknown>
  checksum: string
  created_by: string
  created_at: string
  updated_at: string
  disabled_at: string | null
}

/** Project/Task 層の ResourceBinding を作成または置換する request。 */
export interface PutResourceBindingInput {
  scope_level: 'PROJECT_DEFAULT' | 'TASK'
  scope_key: string
  requirement_key: string
  resource_kind: string
  integration_id: string
  capability_version: string
  requested_scope: Record<string, unknown>
}

/** 編集時だけ非機密の接続設定を取得する。 */
export async function loadIntegrationDetails(
  projectId: string, integrationId: string, signal?: AbortSignal,
): Promise<{ integration: IntegrationRecord; config: Record<string, unknown> }> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/integrations/${encodeURIComponent(integrationId)}`,
    { signal },
  )
  if (!isRecord(value) || !isRecord(value.config)) throw new Error('Integration details did not match its contract')
  return { integration: parseIntegration(value.integration), config: value.config }
}

/** 原 revision を照合して接続設定を更新する。 */
export async function updateIntegration(
  projectId: string, integrationId: string,
  input: CreateIntegrationInput & { expected_revision: number }, csrfToken: string, signal?: AbortSignal,
): Promise<IntegrationRecord> {
  return parseIntegration(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/integrations/${encodeURIComponent(integrationId)}`,
    { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input), signal },
  ))
}

/** 原更新日時を照合し、未指定の認証値を保持する。 */
export async function updateSecretReference(
  projectId: string, secretReferenceId: string,
  input: { name: string; key_version: string; expected_updated_at: string; locator?: string; secret_value?: string },
  csrfToken: string, signal?: AbortSignal,
): Promise<SecretReferenceRecord> {
  return parseSecretReference(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/secret-references/${encodeURIComponent(secretReferenceId)}`,
    { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input), signal },
  ))
}

/** 未参照接続を元の revision で削除する。 */
export async function deleteIntegration(
  projectId: string, integrationId: string, expectedRevision: number, csrfToken: string, signal?: AbortSignal,
): Promise<void> {
  await requestApiEmpty(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/integrations/${encodeURIComponent(integrationId)}`
      + `?expected_revision=${expectedRevision}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal },
  )
}

/** 未参照の認証情報と密文を原更新日時を照合して削除する。 */
export async function deleteSecretReference(
  projectId: string, secretReferenceId: string, expectedUpdatedAt: string, csrfToken: string, signal?: AbortSignal,
): Promise<void> {
  await requestApiEmpty(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/secret-references/${encodeURIComponent(secretReferenceId)}`
      + `?expected_updated_at=${encodeURIComponent(expectedUpdatedAt)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal },
  )
}

/** Project の SecretReference metadata を一覧する。 */
export async function loadSecretReferences(
  projectId: string,
  signal?: AbortSignal,
): Promise<SecretReferenceRecord[]> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/secret-references`,
    { signal },
  )
  return parseItemList(
    value,
    'items',
    isSecretReference,
    'SecretReference list did not match its contract',
  )
}

/** Secret 値を送らず deployment locator metadata を登録する。 */
export async function createSecretReference(
  projectId: string,
  input: CreateSecretReferenceInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<SecretReferenceRecord> {
  return parseSecretReference(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/secret-references`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** SecretReference を削除せず新規解決対象から外す。 */
export async function disableSecretReference(
  projectId: string,
  secretReferenceId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<SecretReferenceRecord> {
  return parseSecretReference(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/secret-references/`
      + `${encodeURIComponent(secretReferenceId)}/disable`,
    { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** Project の Integration metadata を一覧する。 */
export async function loadIntegrations(
  projectId: string,
  signal?: AbortSignal,
): Promise<IntegrationRecord[]> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/integrations`,
    { signal },
  )
  return parseItemList(value, 'items', isIntegration, 'Integration list did not match its contract')
}

/** 登録済み Provider の Project instance を作成する。 */
export async function createIntegration(
  projectId: string,
  input: CreateIntegrationInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<IntegrationRecord> {
  return parseIntegration(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/integrations`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** Optimistic revision を指定して Integration を無効化する。 */
export async function disableIntegration(
  projectId: string,
  integrationId: string,
  expectedRevision: number,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<IntegrationRecord> {
  return parseIntegration(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/integrations/`
      + `${encodeURIComponent(integrationId)}/disable`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ expected_revision: expectedRevision }),
      signal,
    },
  ))
}

/** Project の三層 ResourceBinding を監査一覧として取得する。 */
export async function loadResourceBindings(
  projectId: string,
  signal?: AbortSignal,
): Promise<ResourceBindingRecord[]> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/resource-bindings`,
    { signal },
  )
  return parseItemList(
    value,
    'items',
    isResourceBinding,
    'ResourceBinding list did not match its contract',
  )
}

/** Project default または Task override binding を保存する。 */
export async function putResourceBinding(
  projectId: string,
  input: PutResourceBindingInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ResourceBindingRecord> {
  return parseResourceBinding(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/resource-bindings`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** Unknown JSON を locator-free SecretReference response へ制限する。 */
function parseSecretReference(value: unknown): SecretReferenceRecord {
  if (!isSecretReference(value)) throw new Error('SecretReference did not match its contract')
  return value
}

/** SecretReference 公開 allowlist の形状を検証する。 */
function isSecretReference(value: unknown): value is SecretReferenceRecord {
  return isRecord(value)
    && !('locator' in value)
    && hasStrings(value, [
      'secret_reference_id', 'project_id', 'name', 'provider', 'resolver', 'key_version',
      'status', 'created_by', 'created_at', 'updated_at',
    ])
    && (value.resolver === 'ENVIRONMENT' || value.resolver === 'FILE' || value.resolver === 'MANAGED')
    && (value.status === 'ACTIVE' || value.status === 'DISABLED')
    && (typeof value.disabled_at === 'string' || value.disabled_at === null)
}

/** Unknown JSON を connection-body-free Integration response へ制限する。 */
function parseIntegration(value: unknown): IntegrationRecord {
  if (!isIntegration(value)) throw new Error('Integration did not match its contract')
  return value
}

/** Integration 公開 allowlist の形状を検証する。 */
function isIntegration(value: unknown): value is IntegrationRecord {
  return isRecord(value)
    && !('config' in value)
    && hasStrings(value, [
      'integration_id', 'project_id', 'name', 'kind', 'provider', 'status',
      'created_by', 'created_at', 'updated_at',
    ])
    && (value.status === 'ACTIVE' || value.status === 'DISABLED')
    && Number.isInteger(value.revision)
    && isStringArray(value.capabilities)
    && isStringArray(value.config_keys)
    && isRecord(value.scope)
    && (typeof value.secret_reference_id === 'string' || value.secret_reference_id === null)
    && (typeof value.disabled_at === 'string' || value.disabled_at === null)
}

/** Unknown JSON を checksum-bound ResourceBinding response へ制限する。 */
function parseResourceBinding(value: unknown): ResourceBindingRecord {
  if (!isResourceBinding(value)) throw new Error('ResourceBinding did not match its contract')
  return value
}

/** ResourceBinding の公開 snapshot 形状を検証する。 */
function isResourceBinding(value: unknown): value is ResourceBindingRecord {
  return isRecord(value)
    && hasStrings(value, [
      'binding_id', 'project_id', 'scope_level', 'scope_key', 'requirement_key',
      'resource_kind', 'provider', 'capability_version', 'revision', 'checksum',
      'created_by', 'created_at', 'updated_at',
    ])
    && ['PROJECT_DEFAULT', 'TASK', 'RUN'].includes(value.scope_level as string)
    && (typeof value.integration_id === 'string' || value.integration_id === null)
    && (typeof value.run_id === 'string' || value.run_id === null)
    && (typeof value.source_binding_id === 'string' || value.source_binding_id === null)
    && (typeof value.disabled_at === 'string' || value.disabled_at === null)
    && isRecord(value.scope)
}
