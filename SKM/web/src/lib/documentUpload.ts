import { withInferredContentType, type DocumentUploadBody, type DocumentUploadRecord, type ProjectDocumentRecord } from '../api'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'
import { documentUploadFailure, type DocumentUploadFailure } from './documentFeedback'
import { createIdempotencyKey } from './idempotency'
import { isNonNilUuid } from './validation'

/** Actor/Project と原 key/content の組。手動回復には File が無く、POST できない。 */
export interface OriginalDocumentUpload {
  readonly actorId: string
  readonly projectId: string
  readonly uploadKey: string
  readonly label: string
  readonly body: DocumentUploadBody | null
}

/** 処理件数ではなく各原 upload の確定/未知を保持する。 */
export interface DocumentUploadItem {
  readonly original: OriginalDocumentUpload
  readonly phase: 'queued' | 'sending' | 'unknown' | 'published' | 'refused' | 'closed'
  readonly document: ProjectDocumentRecord | null
  readonly failure: DocumentUploadFailure | null
}

/** 未知解決後も自動継続せず、人の操作まで batch を pause する。 */
export interface DocumentUploadBatch {
  readonly items: readonly DocumentUploadItem[]
  readonly paused: boolean
}

/** 手入力の照会は送信 batch ではなく、終了しても File や未知の原要求を変更しない。 */
export interface DocumentUploadRecovery {
  readonly original: OriginalDocumentUpload
  readonly phase: 'checking' | 'settled' | 'cancelled'
  readonly record: DocumentUploadRecord | null
  readonly failure: DocumentUploadFailure | null
}

/** File 本文は Blob の不変 snapshot とし、path/MIME/name も選択時に一度だけ固定する。 */
export function freezeDocumentUpload(actorId: string, projectId: string, file: File): OriginalDocumentUpload {
  const label = file.webkitRelativePath || file.name
  const segments = label.split('/').filter((part) => part.length > 0)
  const name = segments.at(-1) ?? file.name
  const copy = new File([file], name, { type: file.type, lastModified: file.lastModified })
  const body = Object.freeze({ file: Object.freeze(withInferredContentType(copy, name)), name,
    folder: segments.slice(0, -1).join('/') })
  const uploadKey = createIdempotencyKey()
  if (!isNonNilUuid(uploadKey)) throw new Error('Unable to create an original upload identity')
  return Object.freeze({ actorId, projectId, uploadKey, label, body })
}

/** 後続 404/422/503 でも、原 POST の未知を解消できない分類を明示する。 */
export function uploadIsUncertain(failure: DocumentUploadFailure): boolean {
  return ['uploadUnknown', 'uploadPending', 'uploadKeyConflict', 'uploadClosed'].includes(failure.key)
}

/** 未送信と原結果を別集計し、done を成功数として表示しない。 */
export function uploadCounts(batch: DocumentUploadBatch) {
  const count = (phase: DocumentUploadItem['phase']): number => batch.items.filter((item) => item.phase === phase).length
  return { published: count('published'), refused: count('refused'), unknown: count('unknown'), closed: count('closed'),
    queued: count('queued'), sending: count('sending'), total: batch.items.length }
}

/** 一つの原書込が未確定なら、別 upload/delete に置き換えさせない。 */
export function uploadBatchLocked(batch: DocumentUploadBatch): boolean {
  return batch.paused || batch.items.some((item) => ['queued', 'sending', 'unknown'].includes(item.phase))
}

/** HTTP の期限/現在性は共通 hook に委ね、ここでは upload の意味だけを分類する。 */
export const DOCUMENT_UPLOAD_POLICY: ResourceRequestPolicy<DocumentUploadFailure> = {
  classify: documentUploadFailure,
  readTimeout: { key: 'loadFailed' }, writeTimeout: { key: 'uploadUnknown' },
  blocks: (failure) => uploadIsUncertain(failure) || ['sessionExpired', 'denied', 'archived'].includes(failure.key),
}
