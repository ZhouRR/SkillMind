import { API_BASE, exactFields, isRecord, requestApiJson } from './http'
import { isApiTimestamp, isNonNilUuid, sameUuid } from '../lib/validation'

/** 公開停止の受付記録。blob の停止・削除・quota 返却は表さない。 */
export interface DocumentUploadClosureReceipt {
  upload_key: string
  project_id: string
  document_id: string
  closed_at: string
  publication_state: 'CLOSED'
}

/** 元 upload key の公開だけを停止し、upload 本文を再送しない。 */
export async function closeDocumentUpload(projectId: string, uploadKey: string, csrfToken: string,
  signal?: AbortSignal): Promise<DocumentUploadClosureReceipt> {
  const path = closurePath(projectId, uploadKey)
  signal?.throwIfAborted()
  const value = await requestApiJson(path, { method: 'POST', signal, cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({ confirmation: 'STOP_PUBLICATION' }),
  }, { statuses: [200, 201], validate: () => {} })
  signal?.throwIfAborted()
  return parseClosure(value, projectId, uploadKey)
}

/** 読取で原停止を照合する。未検出は停止 POST の不成立を意味しない。 */
export async function loadDocumentUploadClosure(projectId: string, uploadKey: string,
  signal?: AbortSignal): Promise<DocumentUploadClosureReceipt> {
  const path = closurePath(projectId, uploadKey)
  signal?.throwIfAborted()
  const value = await requestApiJson(path, { signal, cache: 'no-store' }, 200)
  signal?.throwIfAborted()
  return parseClosure(value, projectId, uploadKey)
}

/** 現要求の scope と厳密な公開 shape を同時に検証する。 */
function parseClosure(value: unknown, projectId: string, uploadKey: string): DocumentUploadClosureReceipt {
  if (!isRecord(value) || !exactFields(value, ['upload_key', 'project_id', 'document_id', 'closed_at', 'publication_state'])
    || !isNonNilUuid(value.upload_key) || !sameUuid(value.upload_key, uploadKey)
    || !isNonNilUuid(value.project_id) || !sameUuid(value.project_id, projectId)
    || !isNonNilUuid(value.document_id) || !isApiTimestamp(value.closed_at) || value.publication_state !== 'CLOSED') {
    throw new Error('Document upload closure did not match its contract')
  }
  return value as unknown as DocumentUploadClosureReceipt
}

/** 任意 URL や nil key から停止 endpoint を作らない。 */
function closurePath(projectId: string, uploadKey: string): string {
  if (![projectId, uploadKey].every(isNonNilUuid)) throw new Error('Invalid document upload closure identity')
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}/document-uploads/${encodeURIComponent(uploadKey)}/closure`
}
