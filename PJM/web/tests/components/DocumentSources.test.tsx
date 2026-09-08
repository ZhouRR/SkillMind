import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import example from '../../../contracts/examples/run-detail-documents.v1.json'
import { isRunDocumentSnapshots } from '../../src/api/runResources'
import { DocumentSourceField } from '../../src/components/DocumentSourceField'
import { RunDocumentSnapshots } from '../../src/components/RunDocumentSnapshots'
import { ScheduleDialog } from '../../src/components/ScheduleDialog'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { sourceRequirements } from '../../src/lib/taskDraft'
import { DOCUMENT_IDS, documentTask } from '../fixtures/documentTask'

describe.each(['zh', 'ja', 'en'] as const)('document input and history in %s', (language) => {
  it('requires confirmation even for a single available document', () => {
    const requirement = sourceRequirements(documentTask())[0]!
    requirement.options = requirement.options.slice(1, 2)
    const html = renderToStaticMarkup(<LanguageProvider language={language}><DocumentSourceField requirement={requirement} value="" onChange={() => {}} /></LanguageProvider>)
    expect(html).toContain(MESSAGES[language].workspace.documentSelection.choose)
    expect(html).toContain('value="" selected=""')
    expect(html).not.toContain('All documents')
    expect(html).not.toContain('project-documents ·')
  })

  it('uses the actual shared task input and empty scope in the schedule dialog', () => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}><ScheduleDialog open projectId={example.project_id} csrfToken="fixture" task={documentTask()} onClose={() => {}} onSaved={() => {}} /></LanguageProvider>)
    expect(html).toContain('Objective')
    expect(html).toContain(MESSAGES[language].workspace.documentSelection.mode)
    expect(html).toContain('scheduleConfiguration')
    expect(html).toContain('disabled="" type="submit"')
  })

  it('displays the frozen members, checksum and explicit historical limitations', () => {
    if (!isRunDocumentSnapshots(example.document_snapshots, example.project_id, example.selected_sources)) throw new Error('Invalid shared fixture')
    const html = renderToStaticMarkup(<LanguageProvider language={language}><RunDocumentSnapshots snapshots={example.document_snapshots} /></LanguageProvider>)
    expect(html).toContain('guides/guide.md')
    expect(html).toContain(example.document_snapshots[0]!.snapshot!.checksum)
    expect(html).toContain(MESSAGES[language].runResult.documents.status.FROZEN)
    expect(html).toContain(MESSAGES[language].runResult.documents.legacy)
    expect(html).toContain(MESSAGES[language].runResult.documents.invalid)
  })

  it('marks an unfinished document set without silently switching modes', () => {
    const requirement = sourceRequirements(documentTask())[0]!
    const html = renderToStaticMarkup(<LanguageProvider language={language}><DocumentSourceField requirement={requirement} value={`documents:${DOCUMENT_IDS[0]}`} onChange={() => {}} /></LanguageProvider>)
    expect(html).toContain('value="SET" selected=""')
    expect(html).toContain(MESSAGES[language].workspace.documentSelection.invalid)
  })
})
