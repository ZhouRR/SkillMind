import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { SourceRequirementField } from '../../src/components/TaskLaunchFields'
import { LanguageProvider } from '../../src/i18n'
import { ALL_DOCUMENTS_SELECTION, PROJECT_DOCUMENT_LIBRARY_SELECTION } from '../../src/lib/documentSelection'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { buildTaskDraft, defaultSourceProviders, sourceRequirements } from '../../src/lib/taskDraft'
import { documentTask } from '../fixtures/documentTask'

/** 入力と成果の要求を共存させる UI fixture。実 write の可用性や承認の証拠ではない。 */
function task(required = true) {
  const value = documentTask()
  value.readiness!.requirements.push({
    key: 'outputs', kind: 'document', access: 'write', required, status: 'UNSUPPORTED',
    reason: 'Provider is not installed', capabilities: ['document.write/v1'], selection_guidance: null,
    candidates: [{ key: PROJECT_DOCUMENT_LIBRARY_SELECTION, kind: 'document', provider: 'project-library', label: 'Project document library' }],
  })
  return value
}

describe('document library selection', () => {
  it('requires a separate explicit output choice and retains the original tokens', () => {
    const value = task()
    expect(defaultSourceProviders(value)).toEqual({})
    expect(buildTaskDraft(value, '{}', { documents: ALL_DOCUMENTS_SELECTION })).toBeNull()
    const sources = { documents: ALL_DOCUMENTS_SELECTION, outputs: PROJECT_DOCUMENT_LIBRARY_SELECTION }
    expect(buildTaskDraft(value, '{}', sources)?.sources).toEqual(sources)
    expect(buildTaskDraft(value, '{}', { documents: PROJECT_DOCUMENT_LIBRARY_SELECTION, outputs: PROJECT_DOCUMENT_LIBRARY_SELECTION })).toBeNull()
    expect(buildTaskDraft(value, '{}', { documents: ALL_DOCUMENTS_SELECTION, outputs: ALL_DOCUMENTS_SELECTION })).toBeNull()
  })

  it('rejects a disappeared destination without replacing it, while allowing an unused optional output', () => {
    const value = task()
    value.readiness!.requirements[1]!.candidates = []
    expect(buildTaskDraft(value, '{}', { documents: ALL_DOCUMENTS_SELECTION, outputs: PROJECT_DOCUMENT_LIBRARY_SELECTION })).toBeNull()
    expect(buildTaskDraft(task(false), '{}', { documents: ALL_DOCUMENTS_SELECTION })?.sources).toEqual({ documents: ALL_DOCUMENTS_SELECTION })
  })

  it.each(['zh', 'ja', 'en'] as const)('renders the output as an explicit destination in %s', (language) => {
    const requirement = sourceRequirements(task())[1]!
    const html = renderToStaticMarkup(<LanguageProvider language={language}>
      <SourceRequirementField requirement={requirement} value="" onChange={() => {}} />
    </LanguageProvider>)
    expect(html).toContain(MESSAGES[language].workspace.documentSelection.library)
    expect(html).toContain('value="" selected=""')
    expect(html).toContain(`value="${PROJECT_DOCUMENT_LIBRARY_SELECTION}"`)
    expect(html).not.toContain('documentSourceField')
    expect(html).not.toContain(ALL_DOCUMENTS_SELECTION)
  })
})
