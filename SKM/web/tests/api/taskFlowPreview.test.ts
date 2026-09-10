import { afterEach, describe, expect, it, vi } from 'vitest'
import { API_BASE, loadTaskFlowPreview } from '../../src/api'
import { parseTaskFlowPreview } from '../../src/api/taskFlowPreview'
import { FLOW_PROJECT, FLOW_TARGET, flowPreview, flowWireWithContract } from '../fixtures/taskFlow'
import { isLosslessJsonNumber, stringifyLosslessJson } from '../../src/lib/losslessJson'

/** 任意 JSON の一箇所だけを変え、実 parser がその損傷を拒否することを確認する。 */
function changed(path: string, value: unknown): unknown {
  const body: unknown = flowPreview()
  const parts = path.split('.')
  let target = body as Record<string, unknown>
  for (const part of parts.slice(0, -1)) target = target[part] as Record<string, unknown>
  target[parts.at(-1)!] = value
  return body
}
afterEach(() => vi.unstubAllGlobals())
describe('strict original task flow preview', () => {
  it('GETs only the selected version/task and retains original declarations and external assessment', async () => {
    const payload = flowPreview()
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    const signal = new AbortController().signal
    expect(await loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET, signal)).toEqual(payload)
    expect(fetch).toHaveBeenCalledWith(`${API_BASE}/projects/${FLOW_PROJECT}/skill-versions/${FLOW_TARGET.skill_version_id}/tasks/review/flow-preview`, expect.objectContaining({ signal, credentials: 'same-origin' }))
    expect(fetch.mock.calls[0]?.[1].body).toBeUndefined()
    expect(fetch.mock.calls[0]?.[1].method).toBeUndefined()
  })
  it('does not invent fields or state for NOT_DECLARED', () => {
    const payload = { ...flowPreview(), status: 'NOT_DECLARED', plan: null, blueprint_checksum: null, source_traces: [], readiness: { scope: 'SKILL_BLUEPRINT', assessment: null } }
    expect(parseTaskFlowPreview(payload, FLOW_PROJECT, FLOW_TARGET)).toEqual(payload)
  })
  it('allows absent optional declarations, declared empty values, unrelated trace targets and unassessed readiness', () => {
    const payload = flowPreview()
    payload.readiness.assessment = null
    expect(parseTaskFlowPreview(payload, FLOW_PROJECT, FLOW_TARGET).plan!.task.value).not.toHaveProperty('result_contract')
    expect(payload.plan!.shared.questions).toEqual([])
    expect(payload.plan!.shared.assumptions).toBeNull()
  })
  it.each([
    ['preview_version', 'new'], ['preview_checksum', 'sha256:bad'], ['blueprint_checksum', null],
    ['identity.manifest_checksum', `sha256:${'A'.repeat(64)}`], ['identity.task_id', '00000000-0000-0000-0000-000000000000'],
    ['identity.project_id', FLOW_TARGET.skill_id], ['identity.skill_id', FLOW_TARGET.task_id], ['identity.skill_version_id', FLOW_TARGET.task_id],
    ['identity.task_id', FLOW_TARGET.skill_id], ['identity.task_key', 'other'], ['identity.version', '2.0.0'], ['identity.skill_key', 'other'],
    ['status', 'RUNNING'], ['source_traces', []], ['source_traces.0.target', '/broken~9'], ['source_traces.0.path', '../private'],
    ['source_traces.0.path', 'https://outside.invalid/a'], ['source_traces.0.verification', 'SEMANTICALLY_CORRECT'], ['source_traces.1.line', 2],
    ['plan.task.scope', 'SKILL'], ['plan.shared.scope', 'TASK'], ['plan.task.value.key', 'other'],
    ['plan.task.blueprint_ref', '/tasks/01'], ['plan.task_resources.0.blueprint_ref', '/resource_requirements/3'],
    ['plan.task_resources.0.scope', 'SKILL'], ['plan.task.value.resource_keys', []], ['plan.task.value.resource_keys', ['documents', 'documents']],
    ['plan.task.value.objective', 1], ['plan.task.value.unknown', 'extra'], ['plan.task.value.result_contract', { type: 'object' }],
    ['plan.shared.questions', undefined], ['plan.shared.effect_intents.0.mode', 'execute'], ['plan.shared.effect_intents.0.resource_key', 'missing'],
    ['readiness.scope', 'TASK'], ['readiness.assessment.level', 'READY'], ['readiness.assessment.requirements.1.required', false],
    ['readiness.assessment.requirements.1.access', 'write'], ['readiness.assessment.requirements.1.capabilities', []],
    ['readiness.assessment.requirements.1.kind', 'repository'], ['readiness.assessment.requirements.1.key', 'different'],
    ['readiness.assessment.requirements.1.candidates.0.private', 'forbidden'], ['readiness.assessment.requirements.1.status', 'READ'],
  ])('rejects inconsistent or malformed %s = %j', (path, value) => {
    expect(() => parseTaskFlowPreview(changed(path, value), FLOW_PROJECT, FLOW_TARGET)).toThrow()
  })
  it.each(['success_criteria', 'deliverables'] as const)('rejects repeated task %s keys', (name) => {
    const payload = flowPreview()
    if (name === 'success_criteria') payload.plan!.task.value.success_criteria!.push({ ...payload.plan!.task.value.success_criteria![0]! })
    else payload.plan!.task.value.deliverables!.push({ ...payload.plan!.task.value.deliverables![0]! })
    expect(() => parseTaskFlowPreview(payload, FLOW_PROJECT, FLOW_TARGET)).toThrow()
  })
  it.each(['required_rules', 'recommended_steps', 'quality_criteria', 'prohibited_actions'] as const)('rejects repeated shared %s keys', (name) => {
    const payload = flowPreview(); payload.plan!.shared.guidance![name] = [{ key: 'a', text: 'A' }, { key: 'a', text: 'B' }]
    expect(() => parseTaskFlowPreview(payload, FLOW_PROJECT, FLOW_TARGET)).toThrow()
  })
  it('rejects duplicated readiness instead of applying another resource state', () => {
    const payload = flowPreview(); payload.readiness.assessment!.requirements[1] = payload.readiness.assessment!.requirements[0]!
    expect(() => parseTaskFlowPreview(payload, FLOW_PROJECT, FLOW_TARGET)).toThrow()
  })
  it('rejects an assessment attached to an undeclared blueprint', () => {
    const payload = { ...flowPreview(), status: 'NOT_DECLARED', plan: null, blueprint_checksum: null, source_traces: [] }
    expect(() => parseTaskFlowPreview(payload, FLOW_PROJECT, FLOW_TARGET)).toThrow()
  })
  it.each([201, 202, 206])('requires exact 200, not %s', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(flowPreview()), { status })))
    await expect(loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET)).rejects.toThrow()
  })
  it('rejects deeply nested contracts before unbounded recursion', () => {
    let value: Record<string, unknown> = { type: 'string' }
    for (let index = 0; index < 20; index++) value = { type: 'array', items: value }
    expect(() => parseTaskFlowPreview(changed('plan.task.value.parameter_contract', { ...value, contract_version: 'skillmind.task-contract-draft/v1' }), FLOW_PROJECT, FLOW_TARGET)).toThrow()
  })
  it('rejects aggregate contract field count over 100', () => {
    const fields = Array.from({ length: 51 }, (_, index) => ({ key: `field_${index}`, type: 'string', required: true }))
    const body = { contract_version: 'skillmind.task-contract-draft/v1', type: 'object', fields: ['a', 'b'].map((key) => ({ key, type: 'object', required: true, fields })) }
    expect(() => parseTaskFlowPreview(changed('plan.task.value.parameter_contract', body), FLOW_PROJECT, FLOW_TARGET)).toThrow()
  })
  it('accepts the original numeric contract without pretending to recompute Python serialization', () => {
    const payload = changed('plan.task.value.parameter_contract', { contract_version: 'skillmind.task-contract-draft/v1', type: 'number', minimum: 0.0000001, maximum: 1.0 })
    expect(parseTaskFlowPreview(payload, FLOW_PROJECT, FLOW_TARGET).preview_checksum).toBe(flowPreview().preview_checksum)
  })
})

describe('lossless numeric declarations through the real JSON HTTP boundary', () => {
  const huge = `1${'0'.repeat(100)}`
  const declaration = (type: string, values: string): string => `{"contract_version":"skillmind.task-contract-draft/v1","type":"${type}","enum":${values}}`
  it.each([
    ['unsafe integer neighbors', 'integer', '[9007199254740992,9007199254740993]'],
    ['arbitrary integer versus rounded float', 'number', `[${huge},1e100]`],
    ['integer neighbor versus float', 'number', '[9007199254740993,9007199254740992.0]'],
    ['original scalar spellings', 'number', '[1.0,1e-7,-0,1.7976931348623157e308,5e-324]'],
    ['negative float zero', 'number', '[-0.0,2.0]'],
    ['integer negative zero', 'integer', '[-0,1]'],
    ['ordinary strings', 'string', '["1","true"]'],
    ['ordinary booleans', 'boolean', '[true,false]'],
  ])('preserves %s without round-tripping raw numbers through native JSON', async (_, type, values) => {
    const contract = declaration(type, values)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(flowWireWithContract(contract), { status: 200 })))
    const result = await loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET)
    expect(stringifyLosslessJson(result.plan!.task.value.parameter_contract)).toBe(contract)
    expect(result.identity).toEqual(flowPreview().identity)
    expect(result.source_traces[0]!.line).toBe(3)
    expect(result.readiness.assessment!.requirements[0]!.required).toBe(false)
  })
  it('retains exact bounds and nested result contracts in the same decoded response', async () => {
    const contract = `{"contract_version":"skillmind.task-contract-draft/v1","type":"object","fields":[{"key":"values","type":"array","required":true,"items":{"type":"number","minimum":-9007199254740993,"maximum":${huge},"enum":[9007199254740992,9007199254740993,1.0,1e-7,-0.0]}}]}`
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(flowWireWithContract(contract, 'result_contract'), { status: 200 })))
    const result = await loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET)
    expect(stringifyLosslessJson(result.plan!.task.value.result_contract)).toBe(contract)
  })
  it('keeps unsafe values immutable instead of exposing serializable rounded numbers', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(flowWireWithContract(declaration('integer', '[9007199254740993]')), { status: 200 })))
    const result = await loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET)
    const values = result.plan!.task.value.parameter_contract!.enum as unknown[]
    expect(isLosslessJsonNumber(values[0])).toBe(true)
    expect(Object.isFrozen(values[0])).toBe(true)
    expect(() => JSON.stringify(values[0])).toThrow()
  })
  it.each([
    ['9007199254740992.0', '9007199254740993', true],
    ['9007199254740993', '9007199254740992.0', false],
    [huge, '1e100', true], ['1e100', huge, false],
    ['-1e100', `-${huge}`, true], [`-${huge}`, '-1e100', false],
    ['-0.0', '0', true], ['5e-324', '0', false],
  ])('compares original recursive bounds %s <= %s as %s', async (minimum, maximum, valid) => {
    const inner = `{"key":"value","type":"number","required":true,"minimum":${minimum},"maximum":${maximum}}`
    const raw = `{"contract_version":"skillmind.task-contract-draft/v1","type":"object","fields":[${inner}]}`
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(flowWireWithContract(raw), { status: 200 })))
    const loading = loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET)
    if (valid) expect(stringifyLosslessJson((await loading).plan!.task.value.parameter_contract)).toBe(raw)
    else await expect(loading).rejects.toThrow()
  })
  it.each([[0, 0, true], [2, 1, false], [100000, 100000, true]])('checks original string lengths %s <= %s', async (minimum, maximum, valid) => {
    const raw = `{"contract_version":"skillmind.task-contract-draft/v1","type":"string","min_length":${minimum},"max_length":${maximum}}`
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(flowWireWithContract(raw), { status: 200 })))
    if (valid) await expect(loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET)).resolves.toBeTruthy()
    else await expect(loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET)).rejects.toThrow()
  })
  it.each([
    ['numeric int/float equality', 'number', '[1,1.0]'],
    ['positive/negative zero equality', 'number', '[0,-0.0]'],
    ['unsafe exact float equality', 'number', '[9007199254740992,9007199254740992.0]'],
    ['float in integer', 'integer', '[1.0]'],
    ['boolean in integer', 'integer', '[true]'],
    ['boolean in number', 'number', '[false]'],
    ['string in number', 'number', '["1"]'],
    ['number in string', 'string', '[1]'],
    ['number in boolean', 'boolean', '[0]'],
    ['string in boolean', 'boolean', '["true"]'],
    ['enum on object', 'object', '[1]'],
    ['nonfinite float', 'number', '[1e400]'],
    ['duplicate string', 'string', '["one","one"]'],
  ])('rejects %s in its original declared type', async (_, type, values) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(flowWireWithContract(declaration(type, values)), { status: 200 })))
    await expect(loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET)).rejects.toThrow()
  })
  it('does not let numeric wrappers enter identity, source line, or readiness fields', async () => {
    for (const [original, invalid] of [
      ['"project_id":"' + FLOW_PROJECT + '"', '"project_id":9007199254740993'],
      ['"line":3', '"line":3.0'],
      ['"required":false', '"required":0.0'],
    ]) {
      const raw = flowWireWithContract(declaration('integer', '[9007199254740993]')).replace(original!, invalid!)
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(raw, { status: 200 })))
      await expect(loadTaskFlowPreview(FLOW_PROJECT, FLOW_TARGET)).rejects.toThrow()
    }
  })
})
