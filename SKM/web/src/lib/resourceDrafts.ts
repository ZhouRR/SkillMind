import type { CreateSecretReferenceInput, SecretResolver } from '../api'
import { PROVIDER_FORMS } from './resourceConfig'
import type {
  ForgeKind,
  ScopeDraftEntry,
  RepositoryWriteMode,
  ResourceAccess,
  ResourceProvider,
} from './resourceConfig'

/** 外部製品名は翻訳しないため catalog を通さない固定表示名。 */
export const PROVIDER_LABELS: Record<ResourceProvider, string> = {
  redmine: 'Redmine',
  git: 'Git',
  svn: 'SVN',
}

/** 「凭据を新規登録する」select 値。既存 SecretReference の UUID と衝突しない前綴を使う。 */
export const NEW_CREDENTIAL = '__new__'

/** 「requirement key を手入力する」select 値。 */
export const CUSTOM_REQUIREMENT = '__custom__'

/** 資源設定の 4 区分。同時に一つだけ見せ、複数 form の縦積みを解消する。 */
export type ResourceTab = 'connect' | 'secret' | 'binding' | 'policy'

/** 新規作成 form を載せる弹窗の識別。null は全弹窗が閉じている状態。 */
export type ResourceDialog = ResourceTab

/** 接続 form の草稿。capability/kind は入力させず、Provider と権限档位から導出する。 */
export interface ConnectDraft {
  provider: ResourceProvider
  name: string
  baseUrl: string
  repositoryUri: string
  defaultRevision: string
  credentialChoice: string
  resolver: SecretResolver
  locator: string
  /** MANAGED のときだけ使う明文。送信時に KEK 封入され、平文は保持されない。 */
  secretValue: string
  access: ResourceAccess
  /** 既定は明示 wildcard(不限)。列挙は任意の絞り込み。 */
  issueScope: 'all' | 'list'
  fieldScope: 'all' | 'list'
  issueIds: string
  fieldKeys: string[]
  customFieldKeys: string
  paths: string
  revisions: string
  /** 承認済み変更の落とし方 (repository の write 時のみ)。既定は direct。 */
  writeMode: RepositoryWriteMode
  writeBranchPrefix: string
  forgeKind: ForgeKind
  forgeApiBaseUrl: string
  forgeProject: string
}

/** 高度設定内の凭据事前登録 form 草稿。 */
export interface SecretDraft {
  name: string
  provider: ResourceProvider
  resolver: SecretResolver
  locator: string
  /** MANAGED のときだけ使う明文。送信時に KEK 封入され、平文は保持されない。 */
  secret_value: string
  key_version: string
}

/** Binding form の草稿。requirement は公開 task から選び、範囲は部分集合草稿で持つ。 */
export interface BindingDraft {
  scope_level: 'PROJECT_DEFAULT' | 'TASK'
  taskKey: string
  requirementChoice: string
  requirementCustom: string
  integration_id: string
  capabilityFallback: string
  scopeDraft: ScopeDraftEntry[]
}

/** 低 risk 事前許可 form の草稿。範囲は explicit 必須の部分集合草稿。 */
export interface PolicyDraft {
  integration_id: string
  operation: string
  scopeDraft: ScopeDraftEntry[]
  expires_at: string
}

/** Provider 別の要否に合わせた初期値で接続 wizard の草稿を作る。 */
export function emptyConnectDraft(provider: ResourceProvider): ConnectDraft {
  return {
    provider,
    name: '',
    baseUrl: '',
    repositoryUri: '',
    defaultRevision: 'HEAD',
    credentialChoice: PROVIDER_FORMS[provider].requiresSecret ? NEW_CREDENTIAL : '',
    // 自助接入の黄金路径として、既定は平台托管(直接入力)。ENVIRONMENT/FILE は選択で残す。
    resolver: 'MANAGED',
    locator: '',
    secretValue: '',
    access: 'read',
    issueScope: 'all',
    fieldScope: 'all',
    issueIds: '',
    fieldKeys: [],
    customFieldKeys: '',
    paths: '',
    revisions: 'HEAD',
    writeMode: 'direct',
    writeBranchPrefix: '',
    forgeKind: '',
    forgeApiBaseUrl: '',
    forgeProject: '',
  }
}

/** 凭据登録 form の初期草稿。 */
export const EMPTY_SECRET: SecretDraft = {
  name: '', provider: 'redmine', resolver: 'MANAGED', locator: '', secret_value: '', key_version: 'v1',
}

/** 草稿から server 送信用の SecretReference 入力を作る。MANAGED は明文のみ、他は locator のみ。 */
export function secretInputFromDraft(
  fields: { name: string; provider: string; resolver: SecretResolver; locator: string
    secretValue: string; keyVersion: string },
): CreateSecretReferenceInput {
  const base = { name: fields.name, provider: fields.provider, key_version: fields.keyVersion }
  if (fields.resolver === 'MANAGED') {
    // MANAGED は locator を送らず明文だけを渡す(server が即座に KEK 封入する)。
    return { ...base, resolver: 'MANAGED', secret_value: fields.secretValue }
  }
  return { ...base, resolver: fields.resolver, locator: fields.locator }
}

/** 資源綁定 form の初期草稿。 */
export const EMPTY_BINDING: BindingDraft = {
  scope_level: 'PROJECT_DEFAULT',
  taskKey: '',
  requirementChoice: '',
  requirementCustom: '',
  integration_id: '',
  capabilityFallback: '',
  scopeDraft: [],
}

/** 低 risk 事前許可 form の初期草稿。 */
export const EMPTY_POLICY: PolicyDraft = {
  integration_id: '', operation: 'update_fields', scopeDraft: [], expires_at: '',
}
