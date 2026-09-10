import type { TaskFlowPreviewRecord, TaskFlowPreviewTarget } from '../../src/api'

export const FLOW_PROJECT = '00000000-0000-4000-8000-000000000020'
export const FLOW_ACTOR = '00000000-0000-4000-8000-000000000030'
export const FLOW_TARGET: TaskFlowPreviewTarget = {
  skill_id: '00000000-0000-4000-8000-000000000060', skill_version_id: '00000000-0000-4000-8000-000000000061',
  task_id: '00000000-0000-4000-8000-000000000070', task_key: 'review', skill_key: 'source-review', version: '1.0.0',
}

/** 架空の原宣言。hash は Server identity の fixture で、Web の再計算を模倣しない。 */
export function flowPreview(): TaskFlowPreviewRecord {
  return {
    preview_version: 'skillmind.task-flow-preview/v1',
    identity: { ...FLOW_TARGET, project_id: FLOW_PROJECT, manifest_checksum: `sha256:${'a'.repeat(64)}` },
    status: 'AVAILABLE', blueprint_checksum: `sha256:${'b'.repeat(64)}`, preview_checksum: `sha256:${'c'.repeat(64)}`,
    plan: {
      task: { scope: 'TASK', blueprint_ref: '/tasks/1', value: {
        key: FLOW_TARGET.task_key, capability: 'source.review', objective: 'Compare original material <not-html> without changing it.',
        resource_keys: ['documents'], success_criteria: [{ key: 'traceable', text: 'Every finding cites original evidence.' }],
        deliverables: [{ key: 'report', kind: 'report', description: 'A review with limitations, not a saved attachment.' }],
        parameter_contract: { contract_version: 'skillmind.task-contract-draft/v1', type: 'object', fields: [{ key: 'topic', type: 'string', required: true }] },
      } },
      task_resources: [{ scope: 'TASK', blueprint_ref: '/resource_requirements/1', value: { key: 'documents', kind: 'document', required: true, access: 'read', capabilities: ['document.read/v1'] } }],
      shared: {
        scope: 'SKILL', resource_requirements: [{ scope: 'SKILL', blueprint_ref: '/resource_requirements/0', value: { key: 'repository', kind: 'repository', required: false, access: 'read' } }],
        guidance: { required_rules: [{ key: 'evidence', text: 'Retain original evidence.' }], recommended_steps: [{ key: 'compare', text: 'Consider another independent check.' }], quality_criteria: [], prohibited_actions: [{ key: 'no_write', text: 'Do not modify original sources.' }] },
        interaction_points: [{ key: 'clarify', type: 'CLARIFICATION', condition: 'If the requested scope is unclear.', prompt: 'Which scope should be reviewed?' }],
        effect_intents: [{ key: 'suggest', mode: 'propose', operation: 'Describe a possible follow-up, without applying it.', risk: 'low', approval_mode: 'ask' }],
        execution_preferences: { recommended_profile: 'SUPERVISED', session_split_hints: [], stop_conditions: [] },
        assumptions: null, questions: [],
      },
    },
    source_traces: [
      { target: '/tasks/0/objective', path: 'SKILL.md', line: 3, reason: 'Original note for a different Task.', verification: 'TEXT_SNAPSHOT' },
      { target: '/resource_requirements/1', path: 'assets/example.pdf', line: null, reason: 'Original binary reference.', verification: 'SOURCE_INDEX' },
    ],
    readiness: { scope: 'SKILL_BLUEPRINT', assessment: { level: 'CONFIGURATION_REQUIRED', requirements: [
      { key: 'repository', kind: 'repository', required: false, access: 'read', status: 'UNAVAILABLE', reason: 'No candidate.', capabilities: [], selection_guidance: null, candidates: [] },
      { key: 'documents', kind: 'document', required: true, access: 'read', status: 'AVAILABLE', reason: 'A candidate exists.', capabilities: ['document.read/v1'], selection_guidance: null, candidates: [{ key: 'project-documents:all', kind: 'document', provider: 'project', label: 'Documents' }] },
    ] } },
  }
}

/** 数値を JS に一度も通さず、架空の合法外殻へ原 JSON 契約を埋め込む。 */
export function flowWireWithContract(rawContract: string, name: 'parameter_contract' | 'result_contract' = 'parameter_contract'): string {
  const preview = flowPreview()
  preview.plan!.task.value[name] = { __raw_contract_fixture__: true }
  return JSON.stringify(preview).replace('{"__raw_contract_fixture__":true}', rawContract)
}
