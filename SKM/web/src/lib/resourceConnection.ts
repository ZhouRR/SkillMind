import type { CreateIntegrationInput } from '../api'
import {
  PROVIDER_FORMS, SCOPE_WILDCARD, buildIntegrationConfig, buildIntegrationScope,
  capabilitiesForAccess, findScopeIssue, findWriteConfigIssue, mcpPermissionsForAccess,
  parseListInput, type ResourceProvider, type ScopeIssue, type WriteConfigIssue,
} from './resourceConfig'
import type { ConnectDraft } from './resourceDrafts'

/** 配備済み能力だけを表す。接続 scope/権限の最終検証は Server が行う。 */
export interface ResourceFeatures {
  deferredFeaturesEnabled: boolean
  databaseWritesEnabled: boolean
  gitWritesEnabled: boolean
  mcpToolsEnabled: boolean
}

/** 表示と送信で同じ Provider の write 可否を使い、隠れた草稿値を送らない。 */
export function resourceWriteEnabled(provider: ResourceProvider, features: ResourceFeatures): boolean {
  if (provider === 'mcp') return features.mcpToolsEnabled
  if (provider === 'postgres') return features.databaseWritesEnabled
  if (provider === 'git') return features.gitWritesEnabled || features.deferredFeaturesEnabled
  return features.deferredFeaturesEnabled
}

/** 秘密値を含まない接続 payload。Secret の保存結果は送信直前に参照として付ける。 */
export type PreparedConnection =
  | { valid: true; input: Omit<CreateIntegrationInput, 'secret_reference_id'> }
  | { valid: false; scopeIssue: ScopeIssue }
  | { valid: false; writeIssue: WriteConfigIssue }

/** 構造化草稿を一度だけ検証し、同じ scope/config を新規登録と更新で共有する。 */
export function prepareResourceConnection(draft: ConnectDraft, writeEnabled: boolean): PreparedConnection {
  const form = PROVIDER_FORMS[draft.provider]
  const access = !writeEnabled || form.writeCapability === null ? 'read' : draft.access
  const mcpPermissions = mcpPermissionsForAccess(draft.mcpCatalog, access)
  const scope = buildIntegrationScope(draft.provider, {
    issueIds: draft.issueScope === 'all' ? [SCOPE_WILDCARD] : parseListInput(draft.issueIds),
    fieldKeys: access === 'read_write' && draft.fieldScope === 'list'
      ? [...new Set([...draft.fieldKeys, ...parseListInput(draft.customFieldKeys)])] : [SCOPE_WILDCARD],
    paths: parseListInput(draft.paths), revisions: parseListInput(draft.revisions),
    tables: parseListInput(draft.tables), writeEnabled: access === 'read_write',
    writeColumns: parseListInput(draft.writeColumns), operations: draft.databaseOperations,
    resourceUris: parseListInput(draft.resourceUris), mcpTools: draft.mcpTools, mcpPermissions,
  })
  const scopeIssue = findScopeIssue(draft.provider, scope, access === 'read_write')
  if (scopeIssue) return { valid: false, scopeIssue }
  const write = { writeEnabled: access === 'read_write', writeMode: draft.writeMode,
    writeBranchPrefix: draft.writeBranchPrefix, forgeKind: '' as const, forgeApiBaseUrl: '', forgeProject: '' }
  const writeIssue = findWriteConfigIssue(draft.provider, { defaultRevision: draft.defaultRevision, write })
  if (writeIssue) return { valid: false, writeIssue }
  return { valid: true, input: { name: draft.name, kind: form.kind, provider: draft.provider,
    capabilities: capabilitiesForAccess(draft.provider, access, draft.mcpTools, parseListInput(draft.resourceUris).length > 0),
    scope, config: buildIntegrationConfig(draft.provider, { ...draft, mcpPermissions, write }),
  } }
}
