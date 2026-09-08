import type { PublishedTaskRecord } from '../../src/api'
import { ALL_DOCUMENTS_SELECTION } from '../../src/lib/documentSelection'

export const DOCUMENT_IDS = ['00000000-0000-4000-8000-000000000071', '00000000-0000-4000-8000-000000000072'] as const

/** 共通入力と調度の検証用に、明示文書を要求する公開 task を作る。 */
export function documentTask(required = true): PublishedTaskRecord {
  return {
    skill_id: '00000000-0000-4000-8000-000000000060',
    skill_version_id: '00000000-0000-4000-8000-000000000061',
    skill_key: 'document-analysis', skill_name: 'Document analysis', version: '1.0.0',
    task_key: 'analyze', task_id: '00000000-0000-4000-8000-000000000030',
    title: 'Document analysis', task_type: 'analysis', capability: 'document.analysis/v1',
    input_schema: { type: 'object', properties: { objective: { type: 'string', title: 'Objective' } }, required: ['objective'] },
    output_schema: { type: 'object' }, input_schema_checksum: `sha256:${'a'.repeat(64)}`,
    output_schema_checksum: `sha256:${'b'.repeat(64)}`, task_output_schema: null,
    task_output_schema_checksum: null, workflow: 'default', view: 'standard', default_view: 'standard',
    compatibility_level: 'native', tool_requirements: [], published_at: '2026-09-08T00:00:00Z', last_run: null,
    readiness: {
      level: 'RUNNABLE', requirements: [{
        key: 'documents', kind: 'document', required, access: 'read', status: 'AVAILABLE', reason: 'Available',
        capabilities: ['document.read/v1'], selection_guidance: null,
        candidates: [
          { key: ALL_DOCUMENTS_SELECTION, kind: 'document', provider: 'project-documents', label: 'All documents' },
          ...DOCUMENT_IDS.map((id, index) => ({ key: `document:${id}`, kind: 'document', provider: 'project-documents', label: `guides/${index === 0 ? 'guide' : 'notes'}.md` })),
        ],
      }],
    },
  }
}
