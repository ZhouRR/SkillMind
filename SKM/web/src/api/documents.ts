import { apiTimestampMicroseconds, isApiTimestamp, isNonNilUuid, isUuid, sameUuid } from '../lib/validation'
import { DOCUMENT_PREVIEW_MAX_BYTES } from '../lib/documentPreview'
import {
  API_BASE,
  hasStrings,
  isRecord,
  requestApiEmpty,
  requestApiJson,
  requestApiText,
} from './http'

/** Project 作用域で保存された文書 metadata の公開 record。blob 正文は含まない。 */
export interface ProjectDocumentRecord {
  document_id: string
  project_id: string
  folder: string
  name: string
  size: number
  mime: string
  checksum: string
  uploaded_by: string
  created_at: string
}

/** Project 内で参照可能な文書の一覧 response。 */
export interface DocumentListRecord {
  documents: ProjectDocumentRecord[]
}

/** 原 upload の multipart 内容。選択時に固定し、再描画後の File path を読み直さない。 */
export interface DocumentUploadBody {
  readonly file: File
  readonly name: string
  readonly folder: string
}

/** 現在目录とは独立した原 upload の公開受理記録。削除済み document も原 metadata を持つ。 */
export type DocumentUploadRecord = {
  upload_key: string
  project_id: string
  created_at: string
} & ({ state: 'PENDING'; document: null } | { state: 'PUBLISHED'; document: ProjectDocumentRecord })

/** 原 key を read-only で照合する。404 は将来の受理を否定せず、client は再送しない。 */
export async function loadDocumentUpload(
  projectId: string, uploadKey: string, signal?: AbortSignal,
): Promise<DocumentUploadRecord> {
  if (!isNonNilUuid(uploadKey)) throw new Error('A valid original upload key is required')
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/document-uploads/${encodeURIComponent(uploadKey)}`,
    { signal, cache: 'no-store' }, 200,
  )
  if (!isRecord(value) || Object.keys(value).length !== 5
    || !isUuid(value.upload_key) || !sameUuid(value.upload_key, uploadKey)
    || !isUuid(value.project_id) || !sameUuid(value.project_id, projectId)
    || !isApiTimestamp(value.created_at)) throw new Error('Upload receipt did not match its contract')
  const identity = { upload_key: value.upload_key, project_id: value.project_id, created_at: value.created_at }
  if (value.state === 'PENDING' && value.document === null) return { ...identity, state: 'PENDING', document: null }
  if (value.state === 'PUBLISHED') {
    const document = parseDocument(value.document, projectId)
    if (apiTimestampMicroseconds(document.created_at) < apiTimestampMicroseconds(identity.created_at)) {
      throw new Error('Publication predates the original upload')
    }
    return { ...identity, state: 'PUBLISHED', document }
  }
  throw new Error('Upload receipt did not match its state')
}

/** Project 所有の文書 metadata 一覧を取得する。 */
export async function loadProjectDocuments(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectDocumentRecord[]> {
  return parseDocumentList(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/documents`,
    { signal, cache: 'no-store' }, 200,
  ), projectId).documents
}

/** 元の document ID の現在の metadata だけを取得する。原 DELETE の回执ではない。 */
export async function loadProjectDocument(
  projectId: string, documentId: string, signal?: AbortSignal,
): Promise<ProjectDocumentRecord> {
  return parseDocument(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}`,
    { signal, cache: 'no-store' }, 200,
  ), projectId, documentId)
}

/** OS が MIME を登録しない拡張子(.md 等)の補完表。server allowlist と同じ語彙に限る。 */
const CONTENT_TYPE_BY_EXTENSION: Record<string, string> = {
  md: 'text/markdown',
  markdown: 'text/markdown',
  txt: 'text/plain',
  log: 'text/plain',
  csv: 'text/csv',
  html: 'text/html',
  htm: 'text/html',
  json: 'application/json',
  yaml: 'application/yaml',
  yml: 'application/yaml',
  pdf: 'application/pdf',
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  zip: 'application/zip',
}

/** Browser が空 MIME を付けた file へ拡張子から content type を補う。未知拡張子はそのまま返す。 */
export function withInferredContentType(file: File, name: string): File {
  if (file.type) return file
  const inferred = CONTENT_TYPE_BY_EXTENSION[name.slice(name.lastIndexOf('.') + 1).toLowerCase()]
  return inferred ? new File([file], name, { type: inferred }) : file
}

/** 呼出元の原 key と固定 multipart だけを送信し、内部で key 生成や再送を行わない。 */
export async function uploadProjectDocument(
  projectId: string,
  uploadKey: string,
  body: DocumentUploadBody,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectDocumentRecord> {
  if (!isNonNilUuid(uploadKey)) throw new Error('A valid original upload key is required')
  const form = new FormData()
  // filename は単一 segment にし、親 path は folder field で渡す (server は name に path segment を許可しない)。
  form.append('file', body.file, body.name)
  form.append('folder', body.folder)
  const document = parseDocument(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/documents`,
    {
      // Content-Type は設定しない。browser が multipart boundary 付きで付与する。
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken, 'Idempotency-Key': uploadKey },
      body: form,
      signal,
      cache: 'no-store',
    },
    201,
  ), projectId)
  if (document.size !== body.file.size) throw new Error('Upload response size does not match the original file')
  return document
}

/** 原 ID の metadata 削除 204 のみを受け入れ、blob の完全清理とは解釈しない。 */
export async function deleteProjectDocument(
  projectId: string,
  documentId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<void> {
  await requestApiEmpty(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}`
    + `/documents/${encodeURIComponent(documentId)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal, cache: 'no-store' }, 204,
  )
}

/** 同一 origin cookie 認証の GET で download する文書 content の URL を組み立てる。 */
export function projectDocumentContentHref(projectId: string, documentId: string): string {
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}`
    + `/documents/${encodeURIComponent(documentId)}/content`
}

/** Preview は metadata の size を信用せず、実 stream の byte を共通 HTTP 境界で制限する。 */
export async function loadProjectDocumentText(
  projectId: string,
  documentId: string,
  signal?: AbortSignal,
): Promise<string> {
  return requestApiText(projectDocumentContentHref(projectId, documentId), { signal, cache: 'no-store' },
    { status: 200, maxBytes: DOCUMENT_PREVIEW_MAX_BYTES })
}

/** Unknown JSON を文書一覧の公開 contract へ制限する。 */
function parseDocumentList(value: unknown, projectId: string): DocumentListRecord {
  if (!isRecord(value) || Object.keys(value).length !== 1 || !Array.isArray(value.documents)) {
    throw new Error('Document list response did not match its contract')
  }
  const documents = value.documents.map((item) => parseDocument(item, projectId))
  if (new Set(documents.map((item) => item.document_id.toLowerCase())).size !== documents.length) {
    throw new Error('Document list contains duplicate identities')
  }
  return { documents }
}

/** Unknown JSON を単一文書 record の公開 contract へ制限する。 */
function parseDocument(value: unknown, projectId: string, documentId?: string): ProjectDocumentRecord {
  if (!isDocument(value) || !sameUuid(value.project_id, projectId)
    || documentId !== undefined && !sameUuid(value.document_id, documentId)) {
    throw new Error('Document response did not match its contract')
  }
  return value
}

/** Unknown object が文書 record の公開 field を満たすか確認する。 */
function isDocument(value: unknown): value is ProjectDocumentRecord {
  return isRecord(value)
    && hasStrings(value, [
      'document_id', 'project_id', 'folder', 'name', 'mime', 'checksum',
      'uploaded_by', 'created_at',
    ])
    && typeof value.size === 'number'
    && Number.isSafeInteger(value.size) && value.size >= 0
    && Object.keys(value).length === 9
    && isUuid(value.document_id) && isUuid(value.project_id) && isUuid(value.uploaded_by)
    && isApiTimestamp(value.created_at)
    && /^sha256:[0-9a-f]{64}$/.test(value.checksum as string)
    && Array.from(value.folder as string).length <= 200
    && Array.from(value.name as string).length >= 1 && Array.from(value.name as string).length <= 200
    && Array.from(value.mime as string).length >= 1 && Array.from(value.mime as string).length <= 128
}
