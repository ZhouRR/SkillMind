import {
  API_BASE,
  hasStrings,
  isRecord,
  isStringArray,
  requestApiEmpty,
  requestApiJson,
} from './http'
import { normalizedSkillUploadPaths } from '../lib/skillUpload'

/** Browser から送る Skill source の text file。 */
export interface SkillSourceFile {
  path: string
  content: string
}

/** Parser と Interpreter が共有する診断。Interpreter report では path/line が null になりうる。 */
export interface SkillDiagnostic {
  severity: string
  code: string
  message: string
  path?: string | null
  line?: number | null
}

/** Deterministic parser が返す正規化済み package の最小 UI 契約。 */
export interface NormalizedSkillPackage {
  package_format: string
  source: {
    type: string
    content_hash: string
    detected_adapter: string
    files: Array<Record<string, unknown>>
  }
  metadata: {
    name: string
    description: string
    argument_hint: string | null
  }
  resources: {
    scripts: string[]
    references: string[]
    assets: string[]
  }
  declared_tools: string[]
  diagnostics: SkillDiagnostic[]
}

/** Assisted draft の UI が参照する固定 field。 */
export interface RuntimeManifestDraft {
  identity: {
    skill_key: string
    source_hash: string
    interpreter_version: string
  }
  compatibility: {
    level: string
    confidence: number
    diagnostics: SkillDiagnostic[]
  }
  tools: Array<Record<string, unknown>>
  extensions: {
    normalized_package_hash?: string
    declared_tools?: string[]
  }
}

/** 能力蓝图の一つの説明 note。 */
export interface BlueprintNote {
  key: string
  text: string
}

/** Skill が完成できる一つの領域能力。Tool capability とは別語彙。 */
export interface BlueprintCapability {
  key: string
  title: string
  summary?: string
  intents?: string[]
}

/** 能力から投影される一つの目標。業務 Schema は任意派生物。 */
export interface BlueprintTask {
  key: string
  capability: string
  objective: string
  success_criteria?: BlueprintNote[]
  resource_keys?: string[]
  deliverables?: Array<{ key: string; kind: string; description: string }>
}

/** 実行前に束縛が必要な抽象資源要求。Provider は束縛時に決まる。 */
export interface BlueprintResourceRequirement {
  key: string
  kind: string
  required: boolean
  access: string
  capabilities?: string[]
  accepted_providers?: string[]
  selection_guidance?: string
}

/** Skill 由来の指導を強制度で分類したもの。 */
export interface BlueprintGuidance {
  required_rules: BlueprintNote[]
  recommended_steps: BlueprintNote[]
  quality_criteria: BlueprintNote[]
  prohibited_actions: BlueprintNote[]
}

/** 利用者の関与が必要になる地点。 */
export interface BlueprintInteractionPoint {
  key: string
  type: string
  condition: string
  prompt?: string
}

/** 外部効果の意図。意図であって権限ではない。 */
export interface BlueprintEffectIntent {
  key: string
  mode: string
  operation: string
  risk: string
  resource_key?: string
  approval_mode?: string
}

/** RuntimeManifest から読み取り時に投影される能力蓝图の UI 契約。 */
export interface CapabilityBlueprintView {
  blueprint_version: string
  capabilities: BlueprintCapability[]
  tasks: BlueprintTask[]
  resource_requirements: BlueprintResourceRequirement[]
  guidance: BlueprintGuidance
  interaction_points: BlueprintInteractionPoint[]
  effect_intents: BlueprintEffectIntent[]
}

/**
 * Skill parser API の response。
 *
 * capability_blueprint は解釈前の決定的 draft では null になる。蓝图は Interpreter の産物で
 * あり、導入直後に機械生成で埋めると Skill が申告していない目標と規則を提示することになる。
 */
export interface SkillParseResult {
  normalized_package: NormalizedSkillPackage
  runtime_manifest_draft: RuntimeManifestDraft
  capability_blueprint: CapabilityBlueprintView | null
}

/** 永続化済み SkillSource と interpretation preview。 */
export interface StoredSkillPreviewRecord {
  skill_source_id: string
  interpretation_id: string
  organization_id: string
  name: string
  source_hash: string
  source_type: string
  interpretation_status: string
  compatibility_level: string
  confidence: number
  interpreter_version: string
  created_at: string
  preview: SkillParseResult
}

/** Manifest publish gate の決定的 finding。 */
export interface ManifestGateFindingRecord {
  code: string
  severity: string
  message: string
  path: string | null
}

/** Source と Interpretation に固定された SkillVersion detail。 */
export interface SkillVersionRecord {
  skill_id: string
  skill_version_id: string
  skill_source_id: string
  interpretation_id: string
  organization_id: string
  skill_key: string
  name: string
  /** SKILL.md 由来の説明文。原文の言語をそのまま保持する(空の場合もある)。 */
  description: string
  version: string
  status: 'DRAFT' | 'PUBLISHED' | 'DEPRECATED'
  manifest_checksum: string
  manifest: Record<string, unknown>
  gate_passed: boolean
  gate_findings: ManifestGateFindingRecord[]
  interpretation_diff: Record<string, unknown>
  created_at: string
  published_by: string | null
  published_at: string | null
}

/** Project が明示的に有効化した精確 SkillVersion の監査 record。 */
export interface ProjectSkillVersionRecord {
  project_id: string
  organization_id: string
  skill_version: SkillVersionRecord
  enabled_by: string
  enabled_at: string
  disabled_at: string | null
}

/** ADMIN の CSRF token 付きで Skill source を決定的 parser で解析する。 */
export async function parseSkillSource(
  files: SkillSourceFile[],
  csrfToken: string,
  signal?: AbortSignal,
): Promise<SkillParseResult> {
  return parseSkillParseResult(await requestApiJson(`${API_BASE}/skills/parse`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({ files }),
    signal,
  }))
}

/** ADMIN identity と CSRF token 付きで source snapshot と interpretation を保存する。 */
export async function saveSkillImport(
  files: SkillSourceFile[],
  csrfToken: string,
  signal?: AbortSignal,
): Promise<StoredSkillPreviewRecord> {
  return parseStoredSkillPreview(await requestApiJson(
    `${API_BASE}/skill-imports`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ files }),
      signal,
    },
    201,
  ))
}

/** ADMIN identity と CSRF token 付きで多 file/directory を multipart upload 保存する。 */
export async function uploadSkillFiles(
  files: readonly File[],
  csrfToken: string,
  signal?: AbortSignal,
): Promise<StoredSkillPreviewRecord> {
  const form = new FormData()
  const paths = normalizedSkillUploadPaths(files)
  for (const [index, file] of files.entries()) {
    // Browser が選択 directory 自体の名前を付けるため、Skill root からの相対 path に直す。
    form.append('files', file, paths[index])
  }
  return parseStoredSkillPreview(await requestApiJson(
    `${API_BASE}/skill-imports/upload`,
    {
      // Content-Type は設定しない。browser が multipart boundary 付きで付与する。
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken },
      body: form,
      signal,
    },
    201,
  ))
}

/** Interpretation ID から保存済み assisted preview を再取得する。 */
export async function loadSkillInterpretation(
  interpretationId: string,
  signal?: AbortSignal,
): Promise<StoredSkillPreviewRecord> {
  return parseStoredSkillPreview(await requestApiJson(
    `${API_BASE}/skill-interpretations/${encodeURIComponent(interpretationId)}`,
    { signal },
  ))
}

/** ADMIN の CSRF token 付きで保存済み Interpretation から frozen DRAFT を作成する。 */
export async function createSkillVersionDraft(
  interpretationId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<SkillVersionRecord> {
  return parseSkillVersion(await requestApiJson(
    `${API_BASE}/skill-interpretations/${encodeURIComponent(interpretationId)}/draft`,
    { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** Organization-scoped SkillVersion detail を再取得する。 */
export async function loadSkillVersion(
  skillVersionId: string,
  signal?: AbortSignal,
): Promise<SkillVersionRecord> {
  return parseSkillVersion(await requestApiJson(
    `${API_BASE}/skill-versions/${encodeURIComponent(skillVersionId)}`,
    { signal },
  ))
}

/** ADMIN identity と CSRF token 付きで hard gate の下 SkillVersion を公開する。 */
export async function publishSkillVersion(
  skillVersionId: string,
  acceptedWarnings: string[],
  csrfToken: string,
  signal?: AbortSignal,
): Promise<SkillVersionRecord> {
  return parseSkillVersion(await requestApiJson(
    `${API_BASE}/skill-versions/${encodeURIComponent(skillVersionId)}/publish`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ accepted_warnings: acceptedWarnings }),
      signal,
    },
  ))
}

/** Organization library の全 SkillVersion を取得する。 */
export async function listSkillVersions(signal?: AbortSignal): Promise<SkillVersionRecord[]> {
  const value = await requestApiJson(`${API_BASE}/skill-versions`, { signal })
  if (!isRecord(value) || !Array.isArray(value.skill_versions)) {
    throw new Error('SkillVersion list response did not match its contract')
  }
  return value.skill_versions.map(parseSkillVersion)
}

/** ADMIN の CSRF token 付きで PUBLISHED version を廃止する。 */
export async function deprecateSkillVersion(
  skillVersionId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<SkillVersionRecord> {
  return parseSkillVersion(await requestApiJson(
    `${API_BASE}/skill-versions/${encodeURIComponent(skillVersionId)}/deprecate`,
    { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** 監査参照のない DEPRECATED 版を組織 library から物理削除する。
 *
 * Run snapshot などが参照している版は backend が 409 で拒否する。呼び出し側は
 * `ApiProblemError.code` の `skill_version_delete_blocked` を利用者語へ変換する。
 */
export async function deleteSkillVersion(
  skillVersionId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<void> {
  await requestApiEmpty(
    `${API_BASE}/skill-versions/${encodeURIComponent(skillVersionId)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal },
  )
}

/** Project の active または履歴を含む SkillVersion 有効化一覧を取得する。 */
export async function listProjectSkillVersions(
  projectId: string,
  includeDisabled = false,
  signal?: AbortSignal,
): Promise<ProjectSkillVersionRecord[]> {
  const query = includeDisabled ? '?include_disabled=true' : ''
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/skill-versions${query}`,
    { signal },
  )
  if (!isRecord(value) || !Array.isArray(value.skill_versions)) {
    throw new Error('Project SkillVersion list response did not match its contract')
  }
  return value.skill_versions.map(parseProjectSkillVersion)
}

/** ADMIN が Organization 内の PUBLISHED 精確版を Project へ有効化する。 */
export async function enableProjectSkillVersion(
  projectId: string,
  skillVersionId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectSkillVersionRecord> {
  return parseProjectSkillVersion(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/skill-versions/${encodeURIComponent(skillVersionId)}`,
    { method: 'PUT', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** ADMIN が監査行を残したまま Project の精確版を停用する。 */
export async function disableProjectSkillVersion(
  projectId: string,
  skillVersionId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectSkillVersionRecord> {
  return parseProjectSkillVersion(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/skill-versions/${encodeURIComponent(skillVersionId)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** Unknown JSON を frozen SkillVersion detail contract へ制限する。 */
function parseSkillVersion(value: unknown): SkillVersionRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, [
      'skill_id', 'skill_version_id', 'skill_source_id', 'interpretation_id', 'organization_id',
      'skill_key', 'name', 'description', 'version', 'status', 'manifest_checksum',
      'created_at',
    ])
    || !['DRAFT', 'PUBLISHED', 'DEPRECATED'].includes(value.status as string)
    || !isRecord(value.manifest)
    || typeof value.gate_passed !== 'boolean'
    || !Array.isArray(value.gate_findings)
    || !value.gate_findings.every(isManifestGateFinding)
    || !isRecord(value.interpretation_diff)
    || (typeof value.published_by !== 'string' && value.published_by !== null)
    || (typeof value.published_at !== 'string' && value.published_at !== null)
  ) {
    throw new Error('SkillVersion response did not match its contract')
  }
  return value as unknown as SkillVersionRecord
}

/** Unknown JSON を Project SkillVersion 有効化 contract へ制限する。 */
function parseProjectSkillVersion(value: unknown): ProjectSkillVersionRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, [
      'project_id', 'organization_id', 'enabled_by', 'enabled_at',
    ])
    || (typeof value.disabled_at !== 'string' && value.disabled_at !== null)
  ) {
    throw new Error('Project SkillVersion response did not match its contract')
  }
  return {
    ...value,
    skill_version: parseSkillVersion(value.skill_version),
  } as unknown as ProjectSkillVersionRecord
}

/** Unknown object が gate finding の公開 field を満たすか確認する。 */
function isManifestGateFinding(value: unknown): value is ManifestGateFindingRecord {
  return isRecord(value)
    && hasStrings(value, ['code', 'severity', 'message'])
    && (typeof value.path === 'string' || value.path === null)
}

/** Unknown JSON を Skill parser response の UI 最小契約へ制限する。 */
function parseSkillParseResult(value: unknown): SkillParseResult {
  if (!isRecord(value)) throw new Error('Skill parse response did not match its contract')
  const normalizedPackage = value.normalized_package
  const manifestDraft = value.runtime_manifest_draft
  if (!isNormalizedPackage(normalizedPackage) || !isRuntimeManifestDraft(manifestDraft)) {
    throw new Error('Skill parse response did not match its contract')
  }
  return {
    normalized_package: normalizedPackage,
    runtime_manifest_draft: manifestDraft,
    capability_blueprint: parseOptionalBlueprint(value.capability_blueprint),
  }
}

/**
 * 未解釈を表す null と、契約違反の壊れた蓝图とを区別する。
 *
 * 両方を null へ畳むと、Interpreter が壊れた蓝图を返しても「まだ解釈していない」と同じ表示に
 * なり、欠陥が UI 上で無害に見えてしまう。
 */
function parseOptionalBlueprint(value: unknown): CapabilityBlueprintView | null {
  if (value === null || value === undefined) return null
  if (!isCapabilityBlueprint(value)) {
    throw new Error('CapabilityBlueprint did not match its contract')
  }
  return value
}

/** Unknown JSON を保存済み Skill preview response へ制限する。 */
function parseStoredSkillPreview(value: unknown): StoredSkillPreviewRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, [
      'skill_source_id',
      'interpretation_id',
      'organization_id',
      'name',
      'source_hash',
      'source_type',
      'interpretation_status',
      'compatibility_level',
      'interpreter_version',
      'created_at',
    ])
    || typeof value.confidence !== 'number'
  ) {
    throw new Error('Stored skill preview response did not match its contract')
  }
  return {
    ...value,
    preview: parseSkillParseResult(value.preview),
  } as unknown as StoredSkillPreviewRecord
}

/** Normalized package の UI 表示に必要な field だけを検証する。 */
function isNormalizedPackage(value: unknown): value is NormalizedSkillPackage {
  if (!isRecord(value) || value.package_format !== 'skillmind.normalized/v1') return false
  const source = value.source
  const metadata = value.metadata
  const resources = value.resources
  return (
    isRecord(source)
    && hasStrings(source, ['type', 'content_hash', 'detected_adapter'])
    && Array.isArray(source.files)
    && isRecord(metadata)
    && hasStrings(metadata, ['name', 'description'])
    && (typeof metadata.argument_hint === 'string' || metadata.argument_hint === null)
    && isRecord(resources)
    && isStringArray(resources.scripts)
    && isStringArray(resources.references)
    && isStringArray(resources.assets)
    && isStringArray(value.declared_tools)
    && Array.isArray(value.diagnostics)
  )
}

/** RuntimeManifest draft の UI 表示に必要な field だけを検証する。 */
function isRuntimeManifestDraft(value: unknown): value is RuntimeManifestDraft {
  if (!isRecord(value)) return false
  const identity = value.identity
  const compatibility = value.compatibility
  const extensions = value.extensions
  return (
    isRecord(identity)
    && hasStrings(identity, ['skill_key', 'source_hash', 'interpreter_version'])
    && isRecord(compatibility)
    && hasStrings(compatibility, ['level'])
    && typeof compatibility.confidence === 'number'
    && Array.isArray(compatibility.diagnostics)
    && Array.isArray(value.tools)
    && isRecord(extensions)
  )
}

/** 能力蓝图の UI 表示に必要な最小構造だけを検証する。 */
function isCapabilityBlueprint(value: unknown): value is CapabilityBlueprintView {
  if (!isRecord(value) || value.blueprint_version !== 'skillmind.capability-blueprint/v1') {
    return false
  }
  const guidance = value.guidance
  return (
    Array.isArray(value.capabilities)
    && Array.isArray(value.tasks)
    && Array.isArray(value.resource_requirements)
    && isRecord(guidance)
    && Array.isArray(guidance.required_rules)
    && Array.isArray(guidance.recommended_steps)
    && Array.isArray(guidance.quality_criteria)
    && Array.isArray(guidance.prohibited_actions)
    && Array.isArray(value.interaction_points)
    && Array.isArray(value.effect_intents)
  )
}

/** Interpreter が示した source 依拠。 */
export interface SourceTrace {
  target: string
  path: string
  line: number | null
  reason: string
}

/** Model interpretation report の UI 契約。 */
export interface InterpretationReport {
  report_version: string
  summary: string
  compatibility_level: string
  confidence: Record<string, number>
  assumptions: Array<{ key: string; text: string }>
  questions: Array<{ key: string; text: string; required: boolean }>
  diagnostics: SkillDiagnostic[]
  source_traces: SourceTrace[]
  unmapped_references: Array<{ path: string; reason: string }>
}

/** Model interpretation の preview（model manifest は assisted draft より広い形）。 */
export interface InterpretationPreview {
  normalized_package: NormalizedSkillPackage
  runtime_manifest_draft: Record<string, unknown>
  capability_blueprint: CapabilityBlueprintView | null
}

/** Model interpretation 実行結果と親との revision diff。 */
export interface InterpretationExecutionRecord {
  interpretation_id: string
  skill_source_id: string
  organization_id: string
  status: string
  origin: string
  model: string | null
  interpreter_version: string
  execution_key: string | null
  error_code: string | null
  compatibility_level: string
  confidence: number
  summary: string
  created_at: string
  preview: InterpretationPreview
  report: InterpretationReport | null
  reused: boolean
  parent_interpretation_id: string | null
  adjustment: Record<string, unknown> | null
  diff: Record<string, unknown>
}

/** Interpret/adjust 受理結果。queued は Worker 実行待ちで execution_key の stream を観測する。 */
export interface InterpretationLaunchRecord {
  status: 'stored' | 'queued'
  execution_key: string
  execution: InterpretationExecutionRecord | null
}

/** ADMIN の CSRF token 付きで interpret を受理させる。model 実行は Worker が行う。 */
export async function interpretSkillSource(
  skillSourceId: string,
  csrfToken: string,
  signal?: AbortSignal,
  forceRegenerate = false,
): Promise<InterpretationLaunchRecord> {
  const query = forceRegenerate ? '?force_regenerate=true' : ''
  return parseInterpretationLaunch(await requestApiJson(
    `${API_BASE}/skill-sources/${encodeURIComponent(skillSourceId)}/interpret${query}`,
    { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** ADMIN の調整指示を親 interpretation に折り込み、reinterpretation を Worker job で受理させる。 */
export async function adjustInterpretation(
  interpretationId: string,
  instruction: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<InterpretationLaunchRecord> {
  return parseInterpretationLaunch(await requestApiJson(
    `${API_BASE}/skill-interpretations/${encodeURIComponent(interpretationId)}/adjust`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ instruction }),
      signal,
    },
  ))
}

/** Interpret job の進行 event(prompt/delta/終端)。 */
export interface InterpretEventRecord {
  event: string
  execution_key: string
  occurred_at: string
  data: Record<string, unknown>
}

/** SSE subscription の lifecycle を Component から切り離す。 */
export interface InterpretEventSubscription {
  close(): void
}

/** Interpret job の進行 event として受け付ける named SSE の固定集合。 */
const INTERPRET_EVENT_NAMES = [
  'interpret.queued',
  'interpret.started',
  'interpret.prompt',
  'interpret.delta',
  'interpret.completed',
  'interpret.failed',
] as const

/** execution_key の interpret 進行 event を SSE で購読する。 */
export function subscribeInterpretEvents(
  executionKey: string,
  onEvent: (event: InterpretEventRecord) => void,
  onConnectionError: () => void,
): InterpretEventSubscription {
  const endpoint = `${API_BASE}/skill-interpretations/stream/${encodeURIComponent(executionKey)}`
  const source = new EventSource(endpoint, { withCredentials: true })
  const listener = (event: Event): void => {
    if (!(event instanceof MessageEvent) || typeof event.data !== 'string') return
    try {
      onEvent(parseInterpretEvent(JSON.parse(event.data) as unknown))
    } catch {
      onConnectionError()
    }
  }
  for (const eventName of INTERPRET_EVENT_NAMES) source.addEventListener(eventName, listener)
  source.onerror = onConnectionError
  return { close: () => source.close() }
}

/** Unknown JSON を launch 受理 contract へ制限する。 */
function parseInterpretationLaunch(value: unknown): InterpretationLaunchRecord {
  if (
    !isRecord(value)
    || (value.status !== 'stored' && value.status !== 'queued')
    || typeof value.execution_key !== 'string'
  ) {
    throw new Error('Interpretation launch response did not match its contract')
  }
  const execution = value.execution === null || value.execution === undefined
    ? null
    : parseInterpretationExecution(value.execution)
  return { status: value.status, execution_key: value.execution_key, execution }
}

/** Unknown SSE JSON を interpret 進行 event へ制限する。 */
function parseInterpretEvent(value: unknown): InterpretEventRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, ['event', 'execution_key', 'occurred_at'])
    || !isRecord(value.data)
  ) {
    throw new Error('Interpret event did not match its contract')
  }
  return value as unknown as InterpretEventRecord
}

/** Model interpretation の実行 detail を親との diff 付きで再取得する。 */
export async function loadInterpretationExecution(
  interpretationId: string,
  signal?: AbortSignal,
): Promise<InterpretationExecutionRecord> {
  return parseInterpretationExecution(await requestApiJson(
    `${API_BASE}/skill-interpretations/${encodeURIComponent(interpretationId)}/execution`,
    { signal },
  ))
}

/** Unknown JSON を model interpretation 実行 contract へ制限する。 */
function parseInterpretationExecution(value: unknown): InterpretationExecutionRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, [
      'interpretation_id', 'skill_source_id', 'organization_id', 'status', 'origin',
      'interpreter_version', 'compatibility_level', 'summary', 'created_at',
    ])
    || typeof value.confidence !== 'number'
    || typeof value.reused !== 'boolean'
    || !isNullableString(value.model)
    || !isNullableString(value.error_code)
    || !isNullableString(value.execution_key)
    || !isNullableString(value.parent_interpretation_id)
    || (!isRecord(value.adjustment) && value.adjustment !== null)
    || !isRecord(value.diff)
    || (!isRecord(value.report) && value.report !== null)
  ) {
    throw new Error('Interpretation execution response did not match its contract')
  }
  return {
    ...value,
    preview: parseInterpretationPreview(value.preview),
    report: value.report === null ? null : parseInterpretationReport(value.report),
  } as unknown as InterpretationExecutionRecord
}

/** Model preview の normalized package と能力蓝图を厳密に、manifest を緩く検証する。 */
function parseInterpretationPreview(value: unknown): InterpretationPreview {
  if (
    !isRecord(value)
    || !isNormalizedPackage(value.normalized_package)
    || !isRecord(value.runtime_manifest_draft)
  ) {
    throw new Error('Interpretation execution response did not match its contract')
  }
  return {
    normalized_package: value.normalized_package,
    runtime_manifest_draft: value.runtime_manifest_draft,
    capability_blueprint: parseOptionalBlueprint(value.capability_blueprint),
  }
}

/** Report の最小必須 field を検証し、任意の説明 field を UI 既定値で補う。 */
function parseInterpretationReport(value: unknown): InterpretationReport {
  if (
    !isRecord(value)
    || !hasStrings(value, ['report_version', 'summary', 'compatibility_level'])
    || !Array.isArray(value.diagnostics)
    || !Array.isArray(value.source_traces)
    || (value.confidence !== undefined && !isRecord(value.confidence))
    || (value.assumptions !== undefined && !Array.isArray(value.assumptions))
    || (value.questions !== undefined && !Array.isArray(value.questions))
    || (value.unmapped_references !== undefined && !Array.isArray(value.unmapped_references))
  ) {
    throw new Error('Interpretation report did not match its contract')
  }
  return {
    report_version: value.report_version,
    summary: value.summary,
    compatibility_level: value.compatibility_level,
    confidence: value.confidence ?? {},
    assumptions: value.assumptions ?? [],
    questions: value.questions ?? [],
    diagnostics: value.diagnostics,
    source_traces: value.source_traces,
    unmapped_references: value.unmapped_references ?? [],
  } as InterpretationReport
}

/** String か null だけを許可する。 */
function isNullableString(value: unknown): value is string | null {
  return typeof value === 'string' || value === null
}
