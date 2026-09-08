import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { RunSubmissionPanel } from '../../src/components/RunSubmissionPanel'
import { ApiProblemError } from '../../src/api'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, UI_LANGUAGES } from '../../src/lib/i18n/messages'
import { failedRunSubmission, freezeRunSubmission, sendingRunSubmission } from '../../src/lib/runSubmission'

/** 機微な本文は入力 fixture にだけ置き、待確認 panel への複製を拒否する。 */
function pendingRequest() {
  return sendingRunSubmission(freezeRunSubmission(
    { actorId: 'actor', projectId: 'project' },
    { skillVersionId: 'version', taskKey: 'analyze', taskTitle: 'Analysis', input: { internal_note: 'private-draft-marker' }, sources: {} },
    'analysis/v1', 'request-key',
  ))
}

describe('Run submission feedback', () => {
  it('keeps the form editable but blocks replacing an in-flight request', () => {
    const html = renderToStaticMarkup(<RunSubmissionPanel pending={pendingRequest()} acknowledgePrevious={false} onAcknowledge={vi.fn()} onRetry={vi.fn()} />)
    expect(html).toContain('disabled=""')
    expect(html).not.toContain('type="checkbox"')
    expect(html).not.toContain('private-draft-marker')
  })

  it.each(UI_LANGUAGES)('explains uncertainty, memory loss and explicit replacement in %s', (language) => {
    const messages = MESSAGES[language].workspace.submission
    const pending = failedRunSubmission(pendingRequest(), undefined)
    const html = renderToStaticMarkup(
      <LanguageProvider language={language}>
        <RunSubmissionPanel pending={pending} acknowledgePrevious={false} onAcknowledge={vi.fn()} onRetry={vi.fn()} />
      </LanguageProvider>,
    )
    expect(html).toContain(messages.title)
    expect(html).toContain(messages.phase.unknown)
    expect(html).toContain(messages.memoryOnly)
    expect(html).toContain(messages.acknowledgeNew)
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
    expect(html).not.toContain('private-draft-marker')
  })

  it('does not offer a blind retry for a conflicting or unverifiable historical request', () => {
    const pending = failedRunSubmission(pendingRequest(), new ApiProblemError('Conflict', 409))
    const html = renderToStaticMarkup(<RunSubmissionPanel pending={pending} acknowledgePrevious={false} onAcknowledge={vi.fn()} onRetry={vi.fn()} />)
    expect(html).not.toContain(MESSAGES.zh.workspace.submission.retry)
    expect(html).toContain('type="checkbox"')
    expect(html).toContain(MESSAGES.zh.workspace.submission.history)
  })
})
