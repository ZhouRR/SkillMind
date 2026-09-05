import {
  API_BASE,
  hasStrings,
  isRecord,
  parseItemList,
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

/** Project 所有の文書 metadata 一覧を取得する。 */
export async function loadProjectDocuments(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectDocumentRecord[]> {
  return parseDocumentList(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/documents`,
    { signal },
  )).documents
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

/** ProjectWriteActor の CSRF token 付きで 1 file を multipart upload する。folder は相対 path から導く。 */
export async function uploadProjectDocument(
  projectId: string,
  file: File,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectDocumentRecord> {
  const relativePath = file.webkitRelativePath || file.name
  const segments = relativePath.split('/').filter((segment) => segment.length > 0)
  const name = segments.at(-1) ?? file.name
  const folder = segments.slice(0, -1).join('/')
  const form = new FormData()
  // filename は単一 segment にし、親 path は folder field で渡す (server は name に path segment を許可しない)。
  form.append('file', withInferredContentType(file, name), name)
  form.append('folder', folder)
  return parseDocument(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/documents`,
    {
      // Content-Type は設定しない。browser が multipart boundary 付きで付与する。
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken },
      body: form,
      signal,
    },
  ))
}

/** ProjectWriteActor の CSRF token 付きで文書 metadata と blob を削除する。 */
export async function deleteProjectDocument(
  projectId: string,
  documentId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<void> {
  await requestApiEmpty(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}`
    + `/documents/${encodeURIComponent(documentId)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal },
  )
}

/** 同一 origin cookie 認証の GET で download する文書 content の URL を組み立てる。 */
export function projectDocumentContentHref(projectId: string, documentId: string): string {
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}`
    + `/documents/${encodeURIComponent(documentId)}/content`
}

/** 画面内 preview 用に文書 content を text として取得する。 */
export async function loadProjectDocumentText(
  projectId: string,
  documentId: string,
  signal?: AbortSignal,
): Promise<string> {
  return requestApiText(projectDocumentContentHref(projectId, documentId), { signal })
}

/** Unknown JSON を文書一覧の公開 contract へ制限する。 */
function parseDocumentList(value: unknown): DocumentListRecord {
  return {
    documents: parseItemList(
      value,
      'documents',
      isDocument,
      'Document list response did not match its contract',
    ),
  }
}

/** Unknown JSON を単一文書 record の公開 contract へ制限する。 */
function parseDocument(value: unknown): ProjectDocumentRecord {
  if (!isDocument(value)) throw new Error('Document response did not match its contract')
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
}
