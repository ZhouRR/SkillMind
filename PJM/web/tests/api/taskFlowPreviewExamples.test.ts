import { describe, expect, it } from 'vitest'

import available from '../../../contracts/examples/task-flow-preview.v1.json'
import undeclared from '../../../contracts/examples/task-flow-preview-not-declared.v1.json'
import {
  parseTaskFlowPreview,
  type TaskFlowPreviewRecord,
} from '../../src/api/taskFlowPreview'

/** 後端の実 projector が生成した example を、独立 serializer を挟まず読む。 */
function parse(value: unknown): TaskFlowPreviewRecord {
  return parseTaskFlowPreview(value, available.identity.project_id, available.identity)
}

/** 原 example の型を実 parser で確定し、各語彙回帰だけに独立した複製を渡す。 */
function copyAvailable(): TaskFlowPreviewRecord {
  return structuredClone(parse(available))
}

describe('backend-generated task flow preview examples', () => {
  it.each([
    { status: 'AVAILABLE', example: available },
    { status: 'NOT_DECLARED', example: undeclared },
  ])('accepts the exact backend $status example without rewriting its identity or checksums', ({ example }) => {
    const before = structuredClone(example)
    const result = parse(example)
    expect(result).toEqual(example)
    expect(example).toEqual(before)
    expect(result.preview_checksum).toBe(example.preview_checksum)
    expect(result.blueprint_checksum).toBe(example.blueprint_checksum)
    expect(result.identity).toEqual(example.identity)
  })

  it('keeps the selected Task, declared resource order and explicit Skill-wide complement', () => {
    const result = parse(available)
    expect(result.plan!.task.scope).toBe('TASK')
    expect(result.plan!.task.blueprint_ref).toBe('/tasks/1')
    expect(result.plan!.task_resources.map((item) => item.value.key)).toEqual(['notes', 'source'])
    expect(result.plan!.task_resources.map((item) => item.blueprint_ref)).toEqual([
      '/resource_requirements/1', '/resource_requirements/0',
    ])
    expect(result.plan!.task_resources.map((item) => item.value.required)).toEqual([false, true])
    expect(result.plan!.shared.scope).toBe('SKILL')
    expect(result.plan!.shared.resource_requirements.map((item) => item.value.key)).toEqual(['tracker'])
    expect(result.plan!.shared.resource_requirements[0]!.scope).toBe('SKILL')
    expect(result.plan!.shared.effect_intents![0]!.mode).toBe('apply')
    expect(result.plan!.shared.questions![0]!.required).toBe(false)
  })

  it('preserves original optional omissions and declared empty readiness lists', () => {
    const result = parse(available)
    expect(result.plan!.task.value).not.toHaveProperty('parameter_contract')
    expect(result.plan!.task.value).not.toHaveProperty('result_contract')
    expect(result.plan!.shared.interaction_points![0]).not.toHaveProperty('prompt')
    expect(result.plan!.shared.execution_preferences).not.toHaveProperty('stop_conditions')
    expect(result.plan!.task_resources[0]!.value).not.toHaveProperty('capabilities')
    expect(result.readiness.assessment!.requirements[0]!.capabilities).toEqual([])
    expect(result.readiness.assessment!.requirements[0]!.candidates).toEqual([])
    expect(result.readiness.assessment!.requirements[0]!.selection_guidance).toBeNull()
  })

  it('accepts original Unicode source paths and unrelated Task source traces', () => {
    const result = parse(available)
    expect(result.source_traces.every((item) => item.path === '説明/SKILL.md')).toBe(true)
    expect(result.source_traces.find((item) => item.target === '/tasks/0')).toEqual({
      target: '/tasks/0', path: '説明/SKILL.md', line: null,
      reason: 'Other task', verification: 'TEXT_SNAPSHOT',
    })
    expect(result.source_traces[0]!.verification).toBe('TEXT_SNAPSHOT')
    expect(result.source_traces[0]!.line).toBe(1)
  })

  it('keeps historical NOT_DECLARED distinct from an available empty plan', () => {
    const result = parse(undeclared)
    expect(result.status).toBe('NOT_DECLARED')
    expect(result.plan).toBeNull()
    expect(result.blueprint_checksum).toBeNull()
    expect(result.source_traces).toEqual([])
    expect(result.readiness).toEqual({ scope: 'SKILL_BLUEPRINT', assessment: null })
  })

  it('accepts absent optional declarations separately from valid empty declarations', () => {
    // ここからは公開形状の語彙試験であり、変更後の本文を原 checksum 済みと主張しない。
    const payload = copyAvailable()
    const plan = payload.plan!
    plan.shared.interaction_points = null
    plan.shared.assumptions = null
    plan.shared.questions = []
    plan.shared.execution_preferences = {}
    plan.task.value.success_criteria = []
    plan.task.value.deliverables = []
    const result = parse(payload)
    expect(result.plan!.shared.interaction_points).toBeNull()
    expect(result.plan!.shared.assumptions).toBeNull()
    expect(result.plan!.shared.questions).toEqual([])
    expect(result.plan!.shared.execution_preferences).toEqual({})
    expect(result.plan!.task.value.success_criteria).toEqual([])
    expect(result.plan!.task.value.deliverables).toEqual([])
  })

  it('does not turn absent Task resource keys into invented Task ownership', () => {
    const payload = copyAvailable()
    const plan = payload.plan!
    delete plan.task.value.resource_keys
    plan.shared.resource_requirements = [...plan.task_resources, ...plan.shared.resource_requirements]
      .map((item) => ({ ...item, scope: 'SKILL' as const }))
      .sort((left, right) => Number(left.blueprint_ref.split('/')[2]) - Number(right.blueprint_ref.split('/')[2]))
    plan.task_resources = []
    const result = parse(payload)
    expect(result.plan!.task.value).not.toHaveProperty('resource_keys')
    expect(result.plan!.task_resources).toEqual([])
    expect(result.plan!.shared.resource_requirements.map((item) => item.value.key)).toEqual(['source', 'notes', 'tracker'])
  })

  it.each(['目标😀 日本語 — café', '原文\n第二行\t説明\r\n第三行'])('preserves Unicode and original whitespace: %s', (objective) => {
    const payload = copyAvailable()
    payload.plan!.task.value.objective = objective
    expect(parse(payload).plan!.task.value.objective).toBe(objective)
  })

  it.each([0x00, 0x08, 0x0b, 0x0c, 0x0e, 0x1f, 0x7f])('preserves contract-valid semantic text containing code point %s', (codePoint) => {
    // 純 projector と公開 API はこれらを UTF-8 原文として認める。path の規則とは別物。
    const payload = copyAvailable()
    const text = `before${String.fromCodePoint(codePoint)}after`
    payload.plan!.task.value.objective = text
    payload.plan!.shared.guidance!.recommended_steps![0]!.text = text
    expect(parse(payload).plan!.task.value.objective).toBe(text)
    expect(parse(payload).plan!.shared.guidance!.recommended_steps![0]!.text).toBe(text)
  })

  it.each([0, -0, 0.0000001, -1.25, 1e100, 1.7976931348623157e308])('accepts finite contract numbers without a second canonical serializer: %s', (minimum) => {
    const payload = copyAvailable()
    payload.plan!.task.value.parameter_contract = {
      contract_version: 'projectmind.task-contract-draft/v1', type: 'number', minimum,
    }
    const result = parse(payload)
    expect(result.plan!.task.value.parameter_contract!.minimum).toBe(minimum)
    expect(result.preview_checksum).toBe(available.preview_checksum)
  })

  it('accepts recursive original contracts with finite numbers, booleans and Unicode enum values', () => {
    const payload = copyAvailable()
    payload.plan!.task.value.parameter_contract = {
      contract_version: 'projectmind.task-contract-draft/v1', type: 'object', fields: [
        { key: 'score', type: 'number', required: true, minimum: -0.0000001, maximum: 1.25 },
        { key: 'confirmed', type: 'boolean', required: false, enum: [false, true] },
        { key: 'language', type: 'string', required: false, enum: ['中文', '日本語', 'English'] },
        { key: 'notes', type: 'array', required: false, items: { type: 'string', max_length: 1000 } },
      ],
    }
    expect(parse(payload).plan!.task.value.parameter_contract).toEqual(payload.plan!.task.value.parameter_contract)
  })

  it('rejects current selection guidance that contradicts its original resource declaration', () => {
    const payload = copyAvailable()
    payload.readiness.assessment!.requirements[0]!.selection_guidance = 'Different original guidance'
    expect(() => parse(payload)).toThrow()
  })

  it('rejects an empty readiness reason as required by the public API model', () => {
    const payload = copyAvailable()
    payload.readiness.assessment!.requirements[0]!.reason = ''
    expect(() => parse(payload)).toThrow()
  })

  it('accepts a reason longer than 1000 characters because the public API does not set that cap', () => {
    // 現 catalog は短い固定理由を返す。これは API 語彙の互換性であり実 producer の実績ではない。
    const payload = copyAvailable()
    const reason = 'r'.repeat(1001)
    payload.readiness.assessment!.requirements[0]!.reason = reason
    expect(parse(payload).readiness.assessment!.requirements[0]!.reason).toBe(reason)
  })

  it('accepts a candidate label longer than 4096 characters without a client-only limit', () => {
    // 同様に公開 model の合法境界だけを検証し、実 DB の名称上限とは混同しない。
    const payload = copyAvailable()
    const label = 'l'.repeat(4097)
    payload.readiness.assessment!.requirements[0]!.candidates = [{
      key: 'candidate', kind: 'document', provider: 'project-documents', label,
    }]
    expect(parse(payload).readiness.assessment!.requirements[0]!.candidates[0]!.label).toBe(label)
  })
})
