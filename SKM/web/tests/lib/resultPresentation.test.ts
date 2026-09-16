import { describe, expect, it } from 'vitest'
import { artifactTitle, evidenceTitle, isHtmlReport } from '../../src/lib/resultPresentation'
import type { EvidenceDetail, RunArtifactRecord, RunDocumentSnapshotRecord } from '../../src/api'

const hash = 'sha256:' + '1'.repeat(64)
const sourceHash = 'sha256:' + '2'.repeat(64)
const artifact: RunArtifactRecord = { artifact_ref: 'art_file', project_id: 'project', run_id: 'run',
  tool_call_id: 'tool', evidence_ref: 'ev_file', path: 'output/document-conversions/document/source.md',
  checksum: hash, size_bytes: 100, mime_type: 'text/plain', created_at: '2026-09-17T00:00:00Z' }
const evidence: EvidenceDetail = { evidence_ref: 'ev_file', tool_call_id: 'tool', evidence_type: 'document-conversion',
  source_uri: 'conversion://fixture', source_locator: { path: artifact.path, document_id: 'document', source_checksum: sourceHash },
  content_hash: hash, snapshot_uri: null, excerpt: null, metadata: {}, created_at: artifact.created_at }
const snapshots: RunDocumentSnapshotRecord[] = [{ requirement_key: 'documents', status: 'FROZEN', snapshot: {
  snapshot_version: 'v1', project_id: 'project', requirement_key: 'documents', selection_mode: 'SINGLE', checksum: sourceHash,
  documents: [{ document_id: 'document', folder: 'specs', name: '送信仕様.xlsx', mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', size: 200, content_hash: sourceHash }],
} }]

describe('report presentation identity', () => {
  it('uses the exact conversion evidence and frozen source name without guessing from path', () => {
    expect(artifactTitle(artifact, null, [evidence], snapshots)).toBe('送信仕様.md')
    expect(evidenceTitle(evidence, snapshots)).toBe('送信仕様.xlsx')
  })
  it.each(['evidence_ref', 'tool_call_id', 'content_hash'] as const)('does not borrow another artifact label when %s differs', (field) => {
    expect(artifactTitle(artifact, null, [{ ...evidence, [field]: 'other' }], snapshots)).toBe('source.md')
  })
  it('does not derive a source name from an ID alone or unavailable snapshots', () => {
    expect(artifactTitle(artifact, null, [{ ...evidence, source_locator: { ...evidence.source_locator, source_checksum: 'wrong' } }], snapshots)).toBe('source.md')
    expect(artifactTitle(artifact, null, [evidence], [])).toBe('source.md')
    expect(artifactTitle(artifact, null, [], snapshots)).toBe('source.md')
  })
  it('uses exact table names as evidence labels and preserves opaque fallback', () => {
    expect(evidenceTitle({ ...evidence, source_locator: { table: 'test_automation.test_document' } })).toBe('test_automation.test_document')
    expect(evidenceTitle({ ...evidence, source_locator: {} })).toBe('ev_file')
  })
  it('isolates complete HTML documents but renders report Markdown inline', () => {
    expect(isHtmlReport('<!doctype html><html lang="ja"><body>report</body></html>')).toBe(true)
    expect(isHtmlReport('# report\n\n<script>alert(1)</script>')).toBe(false)
    expect(isHtmlReport('plain report')).toBe(false)
  })
})
