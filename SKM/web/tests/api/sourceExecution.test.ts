import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import example from '../../../contracts/examples/task-flow-preview-source.v1.json'
import { parseSourceExecution, parseTaskFlowPreview, sourceExecutionFromManifest } from '../../src/api/taskFlowPreview'
import { SourceExecutionPreview } from '../../src/components/SourceExecutionPreview'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES } from '../../src/lib/i18n/messages'

describe('source execution previews', () => {
  it('reads the backend declaration without a synthesized plan', () => {
    const preview = parseTaskFlowPreview(example, example.identity.project_id, example.identity)
    expect(preview).toEqual(example)
    expect(preview.plan).toBeNull()
    expect(preview.source_execution?.declaration.tasks).toHaveLength(1)
    expect(renderToStaticMarkup(createElement(SourceExecutionPreview, { preview: parseSourceExecution(example.source_execution) })))
      .toContain('data-source-execution-preview')
  })
  it.each(['zh', 'ja', 'en'] as const)('labels every resource category without blank headings in %s', (language) => {
    const preview = parseSourceExecution(structuredClone(example.source_execution))
    const original = preview.declaration.resource_requirements[0]!
    preview.declaration.resource_requirements = (['issue', 'repository', 'document', 'file', 'knowledge', 'other'] as const)
      .map((kind) => ({ ...original, key: kind, kind }))
    const html = renderToStaticMarkup(createElement(LanguageProvider, {
      language, children: createElement(SourceExecutionPreview, { preview }),
    }))
    for (const resource of preview.declaration.resource_requirements) {
      expect(html).toContain(`${MESSAGES[language].workspace.resourceKind[resource.kind]} · `)
    }
    expect(html).not.toContain('<strong> · ')
  })
  it.each(['version', 'task', 'resource', 'source', 'input', 'mixed'])('rejects corrupted %s instead of an empty success', (mode) => {
    const value = structuredClone(example)
    if (mode === 'version') value.source_execution.declaration.execution_version = 'unknown'
    if (mode === 'task') value.source_execution.declaration.tasks[0]!.key = 'another'
    if (mode === 'resource') value.source_execution.declaration.resource_requirements[0]!.key = 'another'
    if (mode === 'source') value.source_execution.source_documents = []
    if (mode === 'input') value.source_execution.input_contract.type = 'string'
    if (mode === 'mixed') value.status = 'AVAILABLE'
    expect(() => parseTaskFlowPreview(value, value.identity.project_id, value.identity)).toThrow()
  })
  it('distinguishes old imports from a malformed direct declaration', () => {
    expect(sourceExecutionFromManifest({})).toBeNull()
    expect(() => sourceExecutionFromManifest({ skill_execution: null })).toThrow()
    expect(() => sourceExecutionFromManifest({ skill_execution: {}, capability_blueprint: {} })).toThrow()
  })
})
