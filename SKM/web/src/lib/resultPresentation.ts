import type { EvidenceDetail, RunArtifactRecord, RunDocumentSnapshotRecord, RunResultDetail } from '../api'

/** 完全文書の HTML だけを隔離 frame へ渡す。Markdown 内の HTML 断片は原文のまま扱う。 */
export function isHtmlReport(content: string): boolean {
  return /^\s*(?:<!doctype\s+html\b[^>]*>\s*)?<html\b/i.test(content)
}

/** ID と原 hash が一致した凍結文書だけを、変換元の表示名へ利用する。 */
function sourceDocumentName(evidence: EvidenceDetail, snapshots: readonly RunDocumentSnapshotRecord[]): string | null {
  const locator = evidence.source_locator
  const hash = evidence.evidence_type === 'document-conversion' ? locator.source_checksum : evidence.content_hash
  const matches = snapshots.flatMap((item) => item.status === 'FROZEN' ? item.snapshot.documents : [])
    .filter((item) => item.document_id === locator.document_id && item.content_hash === hash)
  const names = [...new Set(matches.map((item) => item.name))]
  return names.length === 1 ? names[0]! : null
}

/** 業務名が無い証拠も正確な table/path を使い、最後だけ原 Evidence ID へ戻る。 */
export function evidenceTitle(evidence: EvidenceDetail, snapshots: readonly RunDocumentSnapshotRecord[] = []): string {
  const name = sourceDocumentName(evidence, snapshots)
  if (name) return name
  for (const key of ['name', 'table', 'path']) {
    const value = evidence.source_locator[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  return evidence.evidence_ref
}

/** 同じ公開 Artifact の原証拠・hash・ToolCall を照合し、path の類似性では結ばない。 */
export function artifactTitle(artifact: RunArtifactRecord, result: RunResultDetail | null,
  evidence: readonly EvidenceDetail[], snapshots: readonly RunDocumentSnapshotRecord[]): string {
  const original = evidence.find((item) => item.evidence_ref === artifact.evidence_ref
    && item.tool_call_id === artifact.tool_call_id && item.content_hash === artifact.checksum
    && item.source_locator.path === artifact.path)
  if (original?.evidence_type === 'document-conversion') {
    const name = sourceDocumentName(original, snapshots)
    if (name) return name.replace(/\.[^.]+$/, '') + '.md'
  }
  const titles = result?.result_kind === 'OUTCOME_ENVELOPE' && Array.isArray(result.data.deliverables)
    ? [...new Set(result.data.deliverables.flatMap((item: unknown) => {
      if (!item || typeof item !== 'object' || !('artifact_ref' in item) || item.artifact_ref !== artifact.artifact_ref
        || !('title' in item) || typeof item.title !== 'string' || !item.title.trim()) return []
      return [item.title]
    }))] : []
  return titles.length === 1 ? titles[0]! : artifact.path.split('/').at(-1) || artifact.path
}

/** 同名の添付を見分けるための保存場所を短く示す。
 *
 *  先頭の output/ と末尾のファイル名を除き、UUID の区画は先頭 8 文字に縮める。
 *  表示名の推測には使わない(名称は artifactTitle の厳密な照合だけが決める)。 */
export function artifactLocation(path: string): string {
  const segments = path.split('/').slice(0, -1)
  if (segments[0] === 'output') segments.shift()
  return segments.map((segment) => /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(segment) ? `${segment.slice(0, 8)}…` : segment).join('/')
}

/** Unknown 公開値を安全な表示文字列へ絞る。 */
export function displayText(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback
}
