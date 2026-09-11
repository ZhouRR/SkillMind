import type { PublishedTaskRecord } from '../api'

/**
 * 資源管理画面の Provider 別 form 定義と scope/config 構築の純 logic。
 *
 * Backend `integrations/domain.py` の PROVIDER_DEFINITIONS と scope 正規化規則の
 * UI 側 mirror であり、裸 JSON 入力を構造化 form へ置き換えるための唯一の変換点。
 * 検証の最終権威は server 側にあり、ここは「確実に弾かれる入力を早期に知らせる」役割に留める。
 */

/** Platform が認識する接続先の種類。backend の Provider registry と一致させる。 */
export type ResourceProvider = 'redmine' | 'git' | 'svn' | 'postgres' | 'mcp'

/** Scope list の予約 token。管理者が明示的に付与した「全許可」を表す(backend と同値)。 */
export const SCOPE_WILDCARD = '*'

/** 画面で選ぶ権限档位。capability ID の直接入力を置き換える。 */
export type ResourceAccess = 'read' | 'read_write'

/** Provider ごとの固定 form 仕様。capability と kind は利用者に入力させない。 */
export interface ProviderFormDefinition {
  provider: ResourceProvider
  kind: 'issue' | 'repository' | 'other'
  readCapability: string
  writeCapability: string | null
  requiresSecret: boolean
  /** ENVIRONMENT resolver の locator 入力例。 */
  environmentLocatorExample: string
  /** FILE resolver の locator 入力例。 */
  fileLocatorExample: string
}

/** Backend PROVIDER_DEFINITIONS の UI mirror。ここ以外に capability 文字列を書かない。 */
export const PROVIDER_FORMS: Record<ResourceProvider, ProviderFormDefinition> = {
  postgres: {
    provider: 'postgres', kind: 'other', readCapability: 'database.read/v1', writeCapability: null,
    requiresSecret: true, environmentLocatorExample: 'POSTGRES_PASSWORD', fileLocatorExample: '/run/secrets/postgres-password',
  },
  mcp: {
    provider: 'mcp', kind: 'other', readCapability: 'mcp.read/v1', writeCapability: null,
    requiresSecret: false, environmentLocatorExample: 'MCP_ACCESS_TOKEN', fileLocatorExample: '/run/secrets/mcp-access-token',
  },
  redmine: {
    provider: 'redmine',
    kind: 'issue',
    readCapability: 'issue.read/v1',
    writeCapability: 'issue.update/v1',
    requiresSecret: true,
    environmentLocatorExample: 'REDMINE_API_KEY',
    fileLocatorExample: '/run/secrets/redmine-api-key',
  },
  git: {
    provider: 'git',
    kind: 'repository',
    readCapability: 'repository.read/v1',
    // §20 で登録された受控書き込み。宣言した Integration だけが承認済み変更の apply 先になる。
    writeCapability: 'repository.write/v1',
    requiresSecret: false,
    environmentLocatorExample: 'GIT_ACCESS_TOKEN',
    fileLocatorExample: '/run/secrets/git-access-token',
  },
  svn: {
    provider: 'svn',
    kind: 'repository',
    readCapability: 'repository.read/v1',
    writeCapability: 'repository.write/v1',
    requiresSecret: true,
    environmentLocatorExample: 'SVN_PASSWORD',
    fileLocatorExample: '/run/secrets/svn-password',
  },
}

/** 画面に並べる Provider の安定順。 */
export const RESOURCE_PROVIDERS: readonly ResourceProvider[] = ['redmine', 'git', 'svn', 'postgres', 'mcp']

/** Redmine write 時に checkbox で提示する代表的な標準 field。自由追記で補える。 */
export const COMMON_REDMINE_FIELD_KEYS: readonly string[] = [
  'status_id',
  'assigned_to_id',
  'priority_id',
  'done_ratio',
  'category_id',
  'fixed_version_id',
  'notes',
]

/** Unknown 文字列を Provider へ絞る。list 表示で契約外値を安全に扱うための narrowing。 */
export function asResourceProvider(value: string): ResourceProvider | null {
  return value === 'redmine' || value === 'git' || value === 'svn' || value === 'postgres' || value === 'mcp' ? value : null
}

/** 改行・カンマ区切りの自由入力を重複なしの値 list へ変換する。 */
export function parseListInput(value: string): string[] {
  const items: string[] = []
  for (const raw of value.split(/[\n,]/)) {
    const item = raw.trim()
    if (item !== '' && !items.includes(item)) items.push(item)
  }
  return items
}

/** 権限档位を versioned capability の組へ写像する。read_write は read を必ず含める。 */
export function capabilitiesForAccess(
  provider: ResourceProvider,
  access: ResourceAccess,
): string[] {
  const form = PROVIDER_FORMS[provider]
  if (access === 'read_write' && form.writeCapability !== null) {
    return [form.readCapability, form.writeCapability]
  }
  return [form.readCapability]
}

/** Backend runs/service の write 判定 mirror。表示と capability 自動選択で共有する。 */
export function isWriteCapability(capability: string): boolean {
  return capability.includes('.update/')
    || capability.includes('.apply/')
    || capability.includes('.write/')
}

/** Integration の capability 組から表示用の権限档位を復元する。 */
export function accessForCapabilities(capabilities: readonly string[]): ResourceAccess {
  return capabilities.some(isWriteCapability) ? 'read_write' : 'read'
}

/** 構造化入力から server の scope allowlist 形へ組み立てる。形は Provider ごとに固定。 */
export function buildIntegrationScope(
  provider: ResourceProvider,
  input: { issueIds: string[]; fieldKeys: string[]; paths: string[]; revisions: string[]; tables?: string[]; resourceUris?: string[] },
): Record<string, string[]> {
  if (provider === 'postgres') return { tables: input.tables ?? [] }
  if (provider === 'mcp') return { resource_uris: input.resourceUris ?? [] }
  if (provider === 'redmine') {
    return { issue_ids: input.issueIds, field_keys: input.fieldKeys }
  }
  return { paths: input.paths, revisions: input.revisions }
}

/** 承認済み変更の落とし方。既定は direct(承認後そのまま既定 branch へ入れる)。 */
export type RepositoryWriteMode = 'direct' | 'branch'

/** PR を開く forge の種別。空文字は「PR を開かない(未設定)」。 */
export type ForgeKind = '' | 'github' | 'gitlab'

/** repository の書き込み設定。read だけの Integration では送らない。 */
export interface RepositoryWriteInput {
  writeEnabled: boolean
  writeMode: RepositoryWriteMode
  writeBranchPrefix: string
  forgeKind: ForgeKind
  forgeApiBaseUrl: string
  forgeProject: string
}

/** 構造化入力から server の非機密 config allowlist 形へ組み立てる。 */
export function buildIntegrationConfig(
  provider: ResourceProvider,
  input: {
    baseUrl: string
    repositoryUri: string
    defaultRevision: string
    host?: string
    port?: string
    database?: string
    username?: string
    sslmode?: string
    serverUrl?: string
    write?: RepositoryWriteInput
  },
): Record<string, string | number> {
  if (provider === 'postgres') return { host: input.host?.trim() ?? '', port: Number(input.port ?? '5432'),
    database: input.database?.trim() ?? '', username: input.username?.trim() ?? '', sslmode: input.sslmode ?? 'verify-full' }
  if (provider === 'mcp') return { server_url: input.serverUrl?.trim() ?? '', transport: 'streamable_http' }
  if (provider === 'redmine') {
    return { base_url: input.baseUrl.trim() }
  }
  const config: Record<string, string> = {
    repository_uri: input.repositoryUri.trim(),
    default_revision: input.defaultRevision.trim() === '' ? 'HEAD' : input.defaultRevision.trim(),
  }
  const write = input.write
  // 書き込みを有効にした Integration だけが落とし方を宣言する。read だけの構成へ write 用の
  // key を混ぜると、後で誤って write capability を足したときに意図しない既定で走ってしまう。
  if (write === undefined || !write.writeEnabled) return config
  config.write_mode = write.writeMode
  if (write.writeMode === 'branch' && write.writeBranchPrefix.trim() !== '') {
    config.write_branch_prefix = write.writeBranchPrefix.trim()
  }
  if (write.forgeKind !== '') {
    config.forge_kind = write.forgeKind
    config.forge_api_base_url = write.forgeApiBaseUrl.trim()
    config.forge_project = write.forgeProject.trim()
  }
  return config
}

/** Server が確実に拒否する書き込み設定を送信前に検出する。null は「送ってよい」。 */
export type WriteConfigIssue =
  | 'direct_requires_branch_name'
  | 'branch_prefix_reserved'
  | 'forge_incomplete'

/**
 * Backend の登録時検証 mirror。fail closed の理由は backend と同じで、
 * 「推測して通す」より「設定時に気づかせる」を選ぶ。
 */
export function findWriteConfigIssue(
  provider: ResourceProvider,
  input: { defaultRevision: string; write: RepositoryWriteInput },
): WriteConfigIssue | null {
  const { write } = input
  if (provider === 'redmine' || !write.writeEnabled) return null
  const revision = input.defaultRevision.trim() === '' ? 'HEAD' : input.defaultRevision.trim()
  // git の direct は「既定 branch そのもの」へ commit する。HEAD は branch 名ではない。
  if (provider === 'git' && write.writeMode === 'direct' && revision.toUpperCase() === 'HEAD') {
    return 'direct_requires_branch_name'
  }
  if (
    write.writeMode === 'branch'
    && write.writeBranchPrefix.trim() !== ''
    && !write.writeBranchPrefix.trim().startsWith(REPOSITORY_WRITE_BRANCH_PREFIX)
  ) {
    return 'branch_prefix_reserved'
  }
  // forge 設定は三つ揃うか一つも無いか。部分設定は「PR を開くつもりが開かない」を生む。
  const forgeFilled = [write.forgeApiBaseUrl.trim(), write.forgeProject.trim()]
  if (write.forgeKind !== '' && forgeFilled.some((item) => item === '')) return 'forge_incomplete'
  if (write.forgeKind === '' && forgeFilled.some((item) => item !== '')) return 'forge_incomplete'
  return null
}

/** Platform が予約する branch namespace。backend の同名定数の UI mirror。 */
export const REPOSITORY_WRITE_BRANCH_PREFIX = 'skillmind/'

/** Server が確実に拒否する scope を送信前に検出する。null は「送ってよい」。 */
export type ScopeIssue = 'issue_ids_required' | 'field_keys_required' | 'paths_required' | 'tables_required' | 'resource_uris_required'

/** Backend normalize_provider_scope の必須条件 mirror。write は明示 field 列を要求する。 */
export function findScopeIssue(
  provider: ResourceProvider,
  scope: Record<string, string[]>,
  writeEnabled: boolean,
): ScopeIssue | null {
  if (provider === 'postgres') return scope.tables?.length ? null : 'tables_required'
  if (provider === 'mcp') return scope.resource_uris?.length ? null : 'resource_uris_required'
  if (provider === 'redmine') {
    if ((scope.issue_ids ?? []).length === 0) return 'issue_ids_required'
    if (writeEnabled && (scope.field_keys ?? []).length === 0) return 'field_keys_required'
    return null
  }
  if ((scope.paths ?? []).length === 0) return 'paths_required'
  return null
}

/** Scope object のうち文字列 list の項だけを安定順で列挙する。checkbox 描画と要約に使う。 */
export interface ScopeEntry {
  key: string
  values: string[]
}

/** Integration scope を checkbox の描画単位へ変換する。契約外の形は黙って落とさず除外する。 */
export function scopeEntries(scope: Record<string, unknown>): ScopeEntry[] {
  return Object.keys(scope).sort().flatMap((key) => {
    const value = scope[key]
    if (!Array.isArray(value) || !value.every((item) => typeof item === 'string')) return []
    return [{ key, values: value as string[] }]
  })
}

/** 一覧の一行に収まる scope 要約を作る。長い列は先頭だけ見せて件数で畳む。 */
export function summarizeScope(
  scope: Record<string, unknown>,
  options?: { maxItems?: number; wildcardLabel?: string },
): string {
  const maxItems = options?.maxItems ?? 3
  return scopeEntries(scope)
    .map((entry) => {
      if (entry.values.includes(SCOPE_WILDCARD)) {
        return `${entry.key}: ${options?.wildcardLabel ?? SCOPE_WILDCARD}`
      }
      const shown = entry.values.slice(0, maxItems).join(', ')
      const rest = entry.values.length - maxItems
      return `${entry.key}: ${entry.values.length === 0 ? '—' : shown}${rest > 0 ? ` +${rest}` : ''}`
    })
    .join(' · ')
}

/** Scope 部分集合入力の 1 key 分の草稿。wildcard 由来の key は自由入力で収窄する。 */
export interface ScopeDraftEntry {
  key: string
  /** Integration 側の許可値。wildcard の場合は ["*"]。 */
  sourceValues: string[]
  wildcardSource: boolean
  /** wildcard を维持するか。事前許可(explicit 必須)では常に false で扱う。 */
  keepAll: boolean
  /** explicit source の checkbox 選択。 */
  picked: string[]
  /** wildcard source を収窄する自由入力(カンマ/改行区切り)。 */
  raw: string
}

/** Integration scope から部分集合入力の初期草稿を作る。既定は「範囲を狭めない」。 */
export function scopeDraftFromScope(
  scope: Record<string, unknown>,
  options: { keepAll: boolean },
): ScopeDraftEntry[] {
  return scopeEntries(scope).map((entry) => {
    const wildcardSource = entry.values.includes(SCOPE_WILDCARD)
    return {
      key: entry.key,
      sourceValues: entry.values,
      wildcardSource,
      keepAll: wildcardSource && options.keepAll,
      picked: wildcardSource ? [] : [...entry.values],
      raw: '',
    }
  })
}

/** 草稿を server へ送る scope 形へ確定する。key 集合は Integration scope と常に一致する。

    収窄の自由入力に紛れた wildcard token は落とす。wildcard の維持は keepAll だけが
    正規の経路であり、事前許可のような explicit 必須の経路が誤って全許可を得るのを防ぐ。 */
export function scopeFromDraft(entries: readonly ScopeDraftEntry[]): Record<string, string[]> {
  return Object.fromEntries(entries.map((entry) => [
    entry.key,
    entry.wildcardSource
      ? (entry.keepAll
        ? [SCOPE_WILDCARD]
        : parseListInput(entry.raw).filter((value) => value !== SCOPE_WILDCARD))
      : [...entry.picked],
  ]))
}

/**
 * Binding が Run 解決時に要求される capability を決定的に選ぶ。
 *
 * Backend runs/service の `_select_binding_capability` の mirror。ここが一致しないと、
 * 保存した binding が Run 作成時に「stale」と拒否されるため、利用者に capability を
 * 選ばせず同じ規則で自動導出する。該当なしは null(保存させない)。
 */
export function selectBindingCapability(
  capabilities: readonly string[],
  options: { hints: readonly string[]; access: 'read' | 'write'; observeCapability: string | null },
): string | null {
  const wantsWrite = options.access === 'write'
  const hinted = options.hints
    .filter((item) => capabilities.includes(item))
    .filter((item) => isWriteCapability(item) === wantsWrite)
  if (hinted.length > 0) return [...hinted].sort()[0] ?? null
  if (
    options.observeCapability !== null
    && capabilities.includes(options.observeCapability)
    && !wantsWrite
  ) {
    return options.observeCapability
  }
  const fallback = capabilities.filter((item) => isWriteCapability(item) === wantsWrite)
  return [...fallback].sort()[0] ?? null
}

/** Binding form の「資源需求」下拉に出す、公開 task 由来の requirement 選択肢。 */
export interface RequirementOption {
  key: string
  /** Integration を絞る硬境界。readiness に kind が無い場合は capability prefix から互換投影する。 */
  kind: string
  access: 'read' | 'write'
  capabilities: string[]
  observeCapability: string | null
  /** この requirement を要求している task の表示名(重複除去済み)。 */
  taskLabels: string[]
}

/** Run preflight の task 選択と同じ識別子。TASK 層 binding の scope_key に一致させる。 */
export function taskScopeKey(task: PublishedTaskRecord): string {
  return `${task.skill_version_id}:${task.task_key}`
}

/** Task 下拉の表示名。技能名と task 標題で識別できるようにする。 */
export function taskOptionLabel(task: PublishedTaskRecord): string {
  return `${task.skill_name} ${task.version} · ${task.title}`
}

/** 要求が宣言した capability から access に対応する一つを決定的に選ぶ。
 *
 * Backend `runs/service.py` の `_declared_capability` mirror。資源要求の宣言元は blueprint
 * 一本であり、以前の manifest `data_sources[].capability` に相当する値をここで導出する。
 */
export function declaredCapability(
  capabilities: readonly string[],
  access: 'read' | 'write',
): string | null {
  const wantsWrite = access === 'write'
  const matching = capabilities
    .filter((capability) => isWriteCapability(capability) === wantsWrite)
    .sort()
  return matching[0] ?? null
}

/** 単一 task の requirement 選択肢。宣言元は readiness(= blueprint 資源要求)のみ。 */
export function requirementOptionsForTask(task: PublishedTaskRecord): RequirementOption[] {
  const label = taskOptionLabel(task)
  const options = new Map<string, RequirementOption>()
  for (const requirement of task.readiness?.requirements ?? []) {
    const access = requirement.access === 'write' ? 'write' : 'read'
    options.set(requirement.key, {
      key: requirement.key,
      kind: requirement.kind,
      access,
      capabilities: [...requirement.capabilities],
      observeCapability: declaredCapability(requirement.capabilities, access),
      taskLabels: [label],
    })
  }
  return [...options.values()]
}

/** Project 全体の requirement 選択肢。同じ key は task 名を併記して一つへ畳む。 */
export function collectRequirementOptions(tasks: readonly PublishedTaskRecord[]): RequirementOption[] {
  const merged = new Map<string, RequirementOption>()
  for (const task of tasks) {
    for (const option of requirementOptionsForTask(task)) {
      const existing = merged.get(option.key)
      if (existing === undefined) {
        merged.set(option.key, option)
        continue
      }
      for (const label of option.taskLabels) {
        if (!existing.taskLabels.includes(label)) existing.taskLabels.push(label)
      }
    }
  }
  return [...merged.values()].sort((a, b) => a.key.localeCompare(b.key))
}
