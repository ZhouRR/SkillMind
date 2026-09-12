import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { TaskFlowPreview } from '../../src/components/TaskFlowPreview'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, type UiLanguage } from '../../src/lib/i18n/messages'
import { flowPreview, flowWireWithContract, FLOW_PROJECT, FLOW_TARGET } from '../fixtures/taskFlow'
import { parseLosslessJson, stringifyLosslessJson } from '../../src/lib/losslessJson'
import { parseTaskFlowPreview } from '../../src/api/taskFlowPreview'

describe('read-only original task flow rendering', () => {
  it.each(['zh', 'ja', 'en'] as UiLanguage[])('separates declarations, skill scope, readiness and source limits in %s', (language) => {
    const preview = flowPreview()
    const html = renderToStaticMarkup(<LanguageProvider language={language}><TaskFlowPreview preview={preview} /></LanguageProvider>)
    const labels = MESSAGES[language].taskFlow
    for (const label of [labels.taskScope, labels.sharedScope, labels.readiness, labels.sources, labels.required_rules, labels.recommended_steps,
      labels.interactionHint, labels.deliverableHint, labels.effectHint, labels.verification.SOURCE_INDEX, labels.verification.TEXT_SNAPSHOT]) expect(html).toContain(label)
    expect(html).toContain('SKILL.md:3'); expect(html).toContain('/tasks/0/objective')
    expect(html).toContain('&lt;not-html&gt;'); expect(html).not.toContain('<not-html>')
    expect(html).not.toMatch(/<button|<form|<input|href=|role="progressbar"|flow_node_ref/)
    expect(html.indexOf('data-flow-task')).toBeLessThan(html.indexOf('data-flow-shared'))
    expect(html.indexOf('data-flow-shared')).toBeLessThan(html.indexOf('data-flow-readiness'))
  })
  it('keeps missing declaration distinct from explicit empty lists', () => {
    const html = renderToStaticMarkup(<TaskFlowPreview preview={flowPreview()} />)
    expect(html).toContain(MESSAGES.zh.taskFlow.notDeclared)
    expect(html).toContain(MESSAGES.zh.taskFlow.declaredEmpty)
  })
  it('links exact or descendant sources without borrowing ancestor or other task traces', () => {
    const preview = flowPreview()
    const reference = '/tasks/1/success_criteria/0'
    preview.source_traces = [
      { target: reference, path: 'exact.md', line: 1, reason: 'exact-source', verification: 'TEXT_SNAPSHOT' },
      { target: `${reference}/text`, path: 'child.md', line: 1, reason: 'descendant-source', verification: 'TEXT_SNAPSHOT' },
      { target: '/tasks/1', path: 'ancestor.md', line: 1, reason: 'ancestor-source', verification: 'TEXT_SNAPSHOT' },
      { target: '/tasks/0/success_criteria/0', path: 'other.md', line: 1, reason: 'other-task-source', verification: 'TEXT_SNAPSHOT' },
    ]
    const html = renderToStaticMarkup(<TaskFlowPreview preview={preview} />)
    expect(html).toContain(`data-flow-item-sources="${reference}"`)
    expect(html.match(/exact-source/g)).toHaveLength(2)
    expect(html.match(/descendant-source/g)).toHaveLength(2)
    expect(html.match(/ancestor-source/g)).toHaveLength(1)
    expect(html.match(/other-task-source/g)).toHaveLength(1)
    expect(html).toContain(MESSAGES.zh.taskFlow.noItemSources)
  })
  it('retains exact original references for objectives, resources, deliverables, interactions, effects and questions', () => {
    const preview = flowPreview()
    preview.plan!.shared.questions = [{ key: 'scope', text: 'Which scope <not-html>?', required: true }]
    const references = ['/tasks/1/objective', '/resource_requirements/1', '/resource_requirements/0',
      '/tasks/1/deliverables/0', '/interaction_points/0', '/effect_intents/0', '/questions/0']
    preview.source_traces = references.map((target, index) => ({
      target, path: `notes/source-${index}.md`, line: 1, reason: `Original <not-html> source-${index}`,
      verification: 'TEXT_SNAPSHOT' as const,
    }))
    preview.source_traces.push({ target: '/tasks/0/objective', path: 'other.md', line: 1,
      reason: 'Unrelated-task-source', verification: 'TEXT_SNAPSHOT' })
    const html = renderToStaticMarkup(<TaskFlowPreview preview={preview} />)
    for (const [index, reference] of references.entries()) {
      expect(html).toContain(`data-flow-item-sources="${reference}"`)
      expect(html.match(new RegExp(`notes/source-${index}.md:1`, 'g'))).toHaveLength(2)
    }
    expect(html.match(/Unrelated-task-source/g)).toHaveLength(1)
    expect(html).not.toContain('<not-html>')
  })
  it('does not describe unassessed readiness as no resource needed', () => {
    const preview = flowPreview(); preview.readiness.assessment = null
    const html = renderToStaticMarkup(<TaskFlowPreview preview={preview} />)
    expect(html).toContain(MESSAGES.zh.taskFlow.unassessed)
    expect(html).not.toContain(MESSAGES.zh.workspace.noResourceNeeded)
  })
  it('does not fill a NOT_DECLARED plan from current schemas or generate nodes', () => {
    const preview = flowPreview(); preview.plan = null; preview.status = 'NOT_DECLARED'; preview.blueprint_checksum = null; preview.source_traces = []; preview.readiness.assessment = null
    const html = renderToStaticMarkup(<TaskFlowPreview preview={preview} />)
    expect(html).toContain(MESSAGES.zh.taskFlow.missing)
    expect(html).not.toContain('data-flow-task'); expect(html).not.toContain('data-flow-shared')
  })
  it.each(['zh', 'ja', 'en'] as UiLanguage[])('shows original numeric tokens, not wrapper metadata or rounded JSON in %s', (language) => {
    const contract = '{"contract_version":"skillmind.task-contract-draft/v1","type":"number","minimum":-9007199254740993,"maximum":1e100,"enum":[9007199254740992,9007199254740993,1.0,1e-7,-0]}'
    const preview = parseTaskFlowPreview(parseLosslessJson(flowWireWithContract(contract)), FLOW_PROJECT, FLOW_TARGET)
    const html = renderToStaticMarkup(<LanguageProvider language={language}><TaskFlowPreview preview={preview} /></LanguageProvider>)
    for (const token of ['9007199254740992', '9007199254740993', '1.0', '1e-7', '-0', '1e100']) expect(html).toContain(token)
    expect(html).not.toContain('&quot;raw&quot;')
    expect(html).not.toContain('[object Object]')
    expect(stringifyLosslessJson(preview.plan!.task.value.parameter_contract)).toBe(contract)
  })
})

it.each(['zh', 'ja', 'en'] as UiLanguage[])('shows source-backed document prerequisites in %s', (language) => {
  const preview = flowPreview()
  preview.plan!.task.value.document_prerequisites = ['register-run']
  preview.source_traces.push({
    target: `${preview.plan!.task.blueprint_ref}/document_prerequisites`, path: 'SKILL.md', line: 3,
    reason: 'Save the initial record before document access.', verification: 'TEXT_SNAPSHOT',
  })
  const html = renderToStaticMarkup(<LanguageProvider language={language}><TaskFlowPreview preview={preview} /></LanguageProvider>)
  expect(html).toContain(MESSAGES[language].skills.documentPrerequisites)
  expect(html).toContain('register-run')
  expect(html).toContain(`data-flow-item-sources="${preview.plan!.task.blueprint_ref}/document_prerequisites"`)
})
