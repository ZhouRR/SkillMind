import { isApiTimestamp, isNonNilUuid, sameUuid } from '../lib/validation'
import { API_BASE, ApiProblemError, exactFields, isRecord, requestApiBlob, requestApiJson } from './http'

export const ARTIFACT_MAX_BYTES = 1_048_576

/** 授権済みの原 Run で公開された添付回执。内部 storage URI は公開しない。 */
export interface RunArtifactRecord {
  artifact_ref: string
  project_id: string
  run_id: string
  tool_call_id: string
  evidence_ref: string
  path: string
  size_bytes: number
  mime_type: 'text/plain'
  checksum: string
  created_at: string
}

/** モデルの URL や相対 path を使わず、公開された原 Project/Run だけから構築する。 */
function artifactsPath(projectId: string, runId: string): string {
  if (!isNonNilUuid(projectId) || !isNonNilUuid(runId)) throw new Error('Invalid artifact scope')
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/artifacts`
}

/** 原 Project/Run の公開索引を厳密に読む。空配列以外を空成功へ補完しない。 */
export async function loadRunArtifacts(projectId: string, runId: string, signal?: AbortSignal): Promise<RunArtifactRecord[]> {
  signal?.throwIfAborted()
  const value = await requestApiJson(artifactsPath(projectId, runId), { signal, cache: 'no-store' }, 200)
  signal?.throwIfAborted()
  if (!Array.isArray(value) || value.length > 100
    || !value.every((item): item is RunArtifactRecord => isRunArtifact(item, projectId, runId))
    || new Set(value.map((item) => item.artifact_ref)).size !== value.length) {
    throw new Error('Artifact index did not match its contract')
  }
  return value
}

/** 保存索引の size/hash と今回の全 byte を照合し、相違や部分応答をダウンロードしない。 */
export async function loadRunArtifactContent(
  projectId: string, runId: string, artifact: RunArtifactRecord, signal?: AbortSignal,
): Promise<Blob> {
  const original = { ...artifact }
  if (!isRunArtifact(original, projectId, runId)) throw new Error('Invalid artifact receipt')
  const blob = await requestApiBlob(`${artifactsPath(projectId, runId)}/${encodeURIComponent(original.artifact_ref)}/content`,
    { signal, cache: 'no-store' }, { status: 200, maxBytes: ARTIFACT_MAX_BYTES, validate: ({ headers }) => {
      if (!/^text\/plain(?:\s*;\s*charset=utf-8)?$/i.test(headers.get('Content-Type') ?? '')
        || !(headers.get('Cache-Control') ?? '').split(',').some((part) => part.trim().toLowerCase() === 'no-store')
        || headers.get('X-Content-Type-Options')?.toLowerCase() !== 'nosniff'
        || !/^attachment(?:;|$)/i.test(headers.get('Content-Disposition') ?? '')) {
        throw new Error('Artifact content headers did not match its contract')
      }
    } })
  signal?.throwIfAborted()
  if (blob.size !== original.size_bytes) throw invalidContent()
  const bytes = await blob.arrayBuffer()
  signal?.throwIfAborted()
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  signal?.throwIfAborted()
  const checksum = `sha256:${Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')}`
  if (checksum !== original.checksum) throw invalidContent()
  // API が現在保証する UTF-8 と固定 MIME を維持し、HTML として開かせない。
  try { new TextDecoder('utf-8', { fatal: true }).decode(bytes) } catch { throw invalidContent() }
  return new Blob([bytes], { type: 'text/plain;charset=utf-8' })
}

/** path は表示と保存名に限り、URL・絶対 path・dot segment を受け入れない。 */
function isOutputPath(value: unknown): value is string {
  return typeof value === 'string' && [...value].length <= 4096 && value.startsWith('output/')
    && !value.includes('\\') && !/[\p{C}\p{Z}]/u.test(value.replaceAll(' ', ''))
    && value.split('/').every((part) => part !== '' && part !== '.' && part !== '..')
}

/** metadata の追加 field、混在 scope、不正 byte 数を信頼済み表示へ通さない。 */
function isRunArtifact(value: unknown, projectId: string, runId: string): value is RunArtifactRecord {
  return isRecord(value) && exactFields(value, ['artifact_ref', 'project_id', 'run_id', 'tool_call_id',
    'evidence_ref', 'path', 'size_bytes', 'mime_type', 'checksum', 'created_at'])
    && typeof value.artifact_ref === 'string' && /^art_[a-zA-Z0-9_-]+$/.test(value.artifact_ref) && value.artifact_ref.length <= 64
    && isNonNilUuid(value.project_id) && sameUuid(value.project_id, projectId)
    && isNonNilUuid(value.run_id) && sameUuid(value.run_id, runId) && isNonNilUuid(value.tool_call_id)
    && typeof value.evidence_ref === 'string' && /^ev_[a-zA-Z0-9_-]+$/.test(value.evidence_ref) && value.evidence_ref.length <= 64
    && isOutputPath(value.path) && typeof value.size_bytes === 'number' && Number.isSafeInteger(value.size_bytes)
    && value.size_bytes >= 0 && value.size_bytes <= ARTIFACT_MAX_BYTES && value.mime_type === 'text/plain'
    && typeof value.checksum === 'string' && /^sha256:[a-f0-9]{64}$/.test(value.checksum) && isApiTimestamp(value.created_at)
}

/** 本文の相違は内部 hash や元 byte を含まない安定分類にする。 */
function invalidContent(): ApiProblemError {
  return new ApiProblemError('Artifact content is invalid', 409, 'artifact_content_invalid')
}
