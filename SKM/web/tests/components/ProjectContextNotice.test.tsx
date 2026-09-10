import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { ProjectContextNotice } from '../../src/components/ProjectContextNotice'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { projectDeleteErrorMessage } from '../../src/pages/ProjectsPage'
import { DEMO_PROJECT } from '../fixtures'

describe('project boundary feedback', () => {
  for (const language of ['zh', 'ja', 'en'] as const) {
    it(`explains unavailable targets uniformly and offers a read-only retry in ${language}`, () => {
      const messages = MESSAGES[language]
      const html = renderToStaticMarkup(<LanguageProvider language={language}>
        <ProjectContextNotice access={{ status: 'unavailable' }} onRefresh={vi.fn()} />
      </LanguageProvider>)
      expect(html).toContain('data-project-context="unavailable"')
      expect(html).toContain('class="panel projectContextNotice"')
      expect(html).toContain('role="alert"')
      expect(html).toContain(messages.app.projectUnavailable)
      expect(html).toContain(messages.runHistory.retry)
    })

    it(`distinguishes archived reads and Schedule deletion conflicts in ${language}`, () => {
      const messages = MESSAGES[language]
      const html = renderToStaticMarkup(<LanguageProvider language={language}>
        <ProjectContextNotice access={{ status: 'ready', project: { ...DEMO_PROJECT, status: 'ARCHIVED' } }} onRefresh={vi.fn()} />
      </LanguageProvider>)
      expect(html).toContain(messages.app.projectArchived)
      expect(html).toContain('data-project-context="archived"')
      expect(html).toContain('class="panel projectContextNotice"')
      const failure = new ApiProblemError('private diagnostic', 409, 'project_delete_blocked_by_schedules')
      expect(projectDeleteErrorMessage(failure, messages)).toBe(messages.projects.deleteBlockedBySchedules)
      expect(projectDeleteErrorMessage(failure, messages)).not.toContain('private diagnostic')
    })
  }

  it('marks loading as busy without offering duplicate reads', () => {
    const html = renderToStaticMarkup(<ProjectContextNotice access={{ status: 'loading' }} onRefresh={vi.fn()} />)
    expect(html).toContain('aria-busy="true"')
    expect(html).not.toContain('<button')
  })

  it('keeps an empty-project notice separate from the page with a read-only retry', () => {
    const html = renderToStaticMarkup(<ProjectContextNotice access={{ status: 'empty' }} onRefresh={vi.fn()} />)
    expect(html).toContain('class="panel projectContextNotice"')
    expect(html).toContain('data-project-context="empty"')
    expect(html).toContain('role="status"')
    expect(html).toContain(MESSAGES.zh.elements.noAccessibleProjects)
    expect(html).toContain(MESSAGES.zh.runHistory.retry)
  })

  it('does not warn for an authorized active project', () => {
    expect(renderToStaticMarkup(<ProjectContextNotice access={{ status: 'ready', project: DEMO_PROJECT }} onRefresh={vi.fn()} />)).toBe('')
  })
})
