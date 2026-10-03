import { describe, expect, it } from 'vitest'
import type { EvidenceDetail } from '../../src/api'
import { evidencePreviewSource } from '../../src/lib/evidencePreview'

/** 原 excerpt は preview 文脈と独立した値として比較する。 */
const evidence: EvidenceDetail = { evidence_ref: 'ev_example', tool_call_id: 'example', evidence_type: 'workspace-file',
  source_uri: 'workspace://example/spec.md', source_locator: {}, content_hash: 'sha256:' + 'a'.repeat(64),
  snapshot_uri: null, excerpt: 'partial | row |', metadata: {}, created_at: '2026-01-01T00:00:00Z' }
const context = { version: 'markdown-table/v1', source: '| Case | Expected |\n| --- | --- |\n| A | Ready |\n',
  header_line_start: 1, line_start: 3, line_end: 3 }

describe('evidence table display context', () => {
  it('uses confirmed context without rewriting the original excerpt', () => {
    const original = { ...evidence, metadata: { excerpt_preview: context } }
    expect(evidencePreviewSource(original)).toBe(context.source)
    expect(original.excerpt).toBe(evidence.excerpt)
  })
  it.each([null, [], { ...context, version: 'unknown' }, { ...context, source: 'x'.repeat(4097) },
    { ...context, line_start: '3' }, { source: context.source }])('keeps the original view for invalid context %j', (value) => {
    expect(evidencePreviewSource({ ...evidence, metadata: { excerpt_preview: value } })).toBeNull()
  })
})
