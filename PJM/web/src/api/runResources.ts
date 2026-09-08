import { isDocumentId, MAX_SELECTED_DOCUMENTS, type DocumentSelectionMode } from '../lib/documentSelection'
import { isRecord } from './http'

/** 内部 binding JSON ではなく、履歴にも公開できる四項目の資源摘要。 */
export interface RunSourceSummary {
  provider?: string | null
  capability?: string | null
  resource_kind?: string | null
  access?: string | null
}

/** 既知の旧 provider 文字列を保持した公開資源 map。 */
export type RunSourceSummaries = Record<string, string | RunSourceSummary>

/** server が検証した凍結文書の metadata。blob locator や取得用権限は含まない。 */
export interface FrozenDocumentRecord {
  document_id: string
  folder: string
  name: string
  mime: string
  size: number
  content_hash: string
}

/** 一つの要求 slot の作成時点の具体的な文書集合。 */
export interface DocumentSnapshotRecord {
  snapshot_version: 'v1'
  project_id: string
  requirement_key: string
  selection_mode: DocumentSelectionMode
  documents: FrozenDocumentRecord[]
  checksum: string
}

/** 歴史欠損と検証失敗を空集合や実行成功へ変換しない公開投影。 */
export type RunDocumentSnapshotRecord = {
  requirement_key: string
} & (
  | { status: 'FROZEN'; snapshot: DocumentSnapshotRecord }
  | { status: 'LEGACY_UNAVAILABLE' | 'INVALID'; snapshot: null }
)

const SUMMARY_FIELDS = ['provider', 'capability', 'resource_kind', 'access'] as const
const DOCUMENT_FIELDS = ['document_id', 'folder', 'name', 'mime', 'size', 'content_hash'] as const
const SNAPSHOT_FIELDS = ['snapshot_version', 'project_id', 'requirement_key', 'selection_mode', 'documents', 'checksum']

/** 公開許可リスト外の内部 field を型 assertion で受け流さない。 */
export function isRunSourceSummaries(value: unknown): value is RunSourceSummaries {
  return isRecord(value) && Object.values(value).every((source) => typeof source === 'string'
    || isRecord(source) && Object.entries(source).every(([key, field]) =>
      SUMMARY_FIELDS.some((name) => name === key) && (field === null || typeof field === 'string')))
}

/** 公開 snapshot の形・同一性・slot 間の整合を検証する。hash の計算と信頼判定は server の責務。 */
export function isRunDocumentSnapshots(
  value: unknown,
  projectId: string,
  sources: RunSourceSummaries,
): value is RunDocumentSnapshotRecord[] {
  if (!Array.isArray(value)) return false
  const slots = new Set<string>()
  const documents = new Map<string, FrozenDocumentRecord>()
  const paths = new Map<string, string>()
  for (const entry of value) {
    if (!isRecord(entry) || !exactFields(entry, ['requirement_key', 'status', 'snapshot'])
      || typeof entry.requirement_key !== 'string' || slots.has(entry.requirement_key)
      || !Object.hasOwn(sources, entry.requirement_key)) return false
    slots.add(entry.requirement_key)
    if (entry.status === 'LEGACY_UNAVAILABLE' || entry.status === 'INVALID') {
      if (entry.snapshot !== null) return false
      continue
    }
    if (entry.status !== 'FROZEN' || !isDocumentSnapshot(entry.snapshot, projectId, entry.requirement_key)) return false
    const source = sources[entry.requirement_key]
    if (typeof source !== 'object' || !source || source.provider !== 'project-documents'
      || source.capability !== 'document.read/v1') return false
    for (const document of entry.snapshot.documents) {
      const previous = documents.get(document.document_id)
      const path = document.folder ? `${document.folder}/${document.name}` : document.name
      if (previous && DOCUMENT_FIELDS.some((field) => previous[field] !== document[field])) return false
      if (paths.has(path) && paths.get(path) !== document.document_id) return false
      documents.set(document.document_id, document)
      paths.set(path, document.document_id)
    }
  }
  return true
}

/** UUID の集合・順序・path 重複と選択 mode を同時に確認する。 */
function isDocumentSnapshot(value: unknown, projectId: string, requirementKey: string): value is DocumentSnapshotRecord {
  if (!isRecord(value) || !exactFields(value, SNAPSHOT_FIELDS)
    || value.snapshot_version !== 'v1' || value.project_id !== projectId
    || !isDocumentId(value.project_id) || value.requirement_key !== requirementKey
    || !['SINGLE', 'SET', 'ALL'].includes(String(value.selection_mode))
    || !isChecksum(value.checksum) || !Array.isArray(value.documents)
    || value.documents.length < 1 || value.documents.length > MAX_SELECTED_DOCUMENTS
    || !value.documents.every(isFrozenDocument)) return false
  if (value.selection_mode === 'SINGLE' && value.documents.length !== 1
    || value.selection_mode === 'SET' && value.documents.length < 2) return false
  const ids = value.documents.map((document) => document.document_id.toLowerCase())
  const paths = value.documents.map((document) => document.folder ? `${document.folder}/${document.name}` : document.name)
  return ids.every((id, index) => index === 0 || ids[index - 1]! < id)
    && new Set(paths).size === paths.length
}

/** metadata は表示専用でも、非正規 path や未知 field の混入を見逃さない。 */
function isFrozenDocument(value: unknown): value is FrozenDocumentRecord {
  if (!isRecord(value) || !exactFields(value, DOCUMENT_FIELDS) || !isDocumentId(value.document_id)
    || typeof value.folder !== 'string' || typeof value.name !== 'string' || !value.name
    || typeof value.mime !== 'string' || !value.mime || !Number.isSafeInteger(value.size)
    || (value.size as number) < 0 || !isChecksum(value.content_hash)) return false
  const path = value.folder ? `${value.folder}/${value.name}` : value.name
  return !value.name.includes('/') && !/[\\\p{C}\p{Zl}\p{Zp}]/u.test(path)
    && path.split('/').every((part) => part !== '' && !['.', '..', '.projectmind'].includes(part))
}

/** 検証値の形のみを確認し、ブラウザで server identity を再計算しない。 */
function isChecksum(value: unknown): value is string {
  return typeof value === 'string' && /^sha256:[a-f0-9]{64}$/.test(value)
}

/** required と追加 field 拒否を一つの判定に揃える。 */
function exactFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  return Object.keys(value).length === fields.length && fields.every((field) => Object.hasOwn(value, field))
}
