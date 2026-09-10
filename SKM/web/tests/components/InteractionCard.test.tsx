import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { InteractionCard } from '../../src/components/InteractionCard'
import { RunResultPanel } from '../../src/components/RunResultPanel'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { INTERACTION_SCOPE, interactionDetail, interactionFixture } from '../fixtures/interaction'

describe('ordinary reply rendering', () => {
  it.each(['zh', 'ja', 'en'] as const)('shows original prompts and explicit choices in %s without selecting a recommendation', (language) => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}><InteractionCard scope={INTERACTION_SCOPE}
      interaction={interactionFixture('CHOICE')} available writable csrfToken="synthetic-session"
      onSessionExpired={vi.fn()} /></LanguageProvider>)
    expect(html).toContain('data-interaction-form')
    expect(html).toContain(MESSAGES[language].runResult.recommendedSuffix)
    expect(html).not.toContain('checked=""')
    expect(html).not.toContain('synthetic-session')
    expect(html).toContain('data-interaction-submit="" disabled=""')
  })

  it('keeps legacy effect approval strictly read-only without an ordinary form', () => {
    const html = renderToStaticMarkup(<InteractionCard scope={INTERACTION_SCOPE} interaction={interactionFixture('EFFECT_APPROVAL')}
      available writable csrfToken="synthetic-session" onSessionExpired={vi.fn()} />)
    expect(html).toContain(MESSAGES.zh.interactionResponse.effectReadOnly)
    expect(html).not.toContain('data-interaction-form')
    expect(html).not.toContain('data-interaction-submit')
  })

  it('retains a same-Run authorized detail while loading but prevents a new answer', () => {
    const html = renderToStaticMarkup(<RunResultPanel actorId={INTERACTION_SCOPE.actorId} projectId={INTERACTION_SCOPE.projectId}
      runId={INTERACTION_SCOPE.runId} csrfToken="synthetic-session" state={{ status: 'loading', detail: interactionDetail() }} />)
    expect(html).toContain('Which public option?')
    expect(html).toContain(MESSAGES.zh.interactionResponse.staleDetail)
    expect(html.match(/data-interaction-card=/g)).toHaveLength(1)
    expect(html).toContain(MESSAGES.zh.interactionResponse.recordsTitle)
    expect(html).not.toContain(MESSAGES.zh.runResult.pendingHint)
  })

  it('does not claim a queued Run is paused just because an old question is still open', () => {
    const detail = { ...interactionDetail(), status: 'QUEUED' as const }
    const html = renderToStaticMarkup(<RunResultPanel actorId={INTERACTION_SCOPE.actorId} projectId={INTERACTION_SCOPE.projectId}
      runId={INTERACTION_SCOPE.runId} csrfToken="synthetic-session" state={{ status: 'ready', detail }} />)
    expect(html).toContain(MESSAGES.zh.interactionResponse.recordsTitle)
    expect(html).not.toContain(MESSAGES.zh.runResult.pendingHint)
  })

  it('does not show a previous Project detail under the current owner', () => {
    const html = renderToStaticMarkup(<RunResultPanel actorId={INTERACTION_SCOPE.actorId} projectId="other-project"
      runId={INTERACTION_SCOPE.runId} csrfToken="synthetic-session" state={{ status: 'loading', detail: interactionDetail() }} />)
    expect(html).not.toContain('data-interaction-card')
  })

  it.each(['zh', 'ja', 'en'] as const)('preserves duplicate-key history as read-only in %s', (language) => {
    const interaction = interactionFixture('CHOICE')
    interaction.options.push({ key: 'a', label: 'Another historical A', description: 'The original second meaning.' })
    interaction.status = 'RESPONDED'
    interaction.response = { response_id: '00000000-0000-4000-8000-000000000070', actor_id: INTERACTION_SCOPE.actorId,
      interaction_version: 3, response: { selected_option_keys: ['a'], text: 'Saved original answer' }, created_at: '2026-01-01T00:01:00Z' }
    const html = renderToStaticMarkup(<LanguageProvider language={language}><InteractionCard scope={INTERACTION_SCOPE}
      interaction={interaction} available writable csrfToken="synthetic-session" onSessionExpired={vi.fn()} /></LanguageProvider>)
    expect(html).toContain(MESSAGES[language].interactionResponse.duplicateOptions)
    for (const text of ['Option A', 'Option B', 'Another historical A', 'The original second meaning.', 'Saved original answer']) {
      expect(html).toContain(text)
    }
    expect(html).toContain('data-interaction-readonly-options')
    expect(html).not.toContain('data-interaction-form')
    expect(html).not.toContain('data-interaction-submit')
  })

  it.each(['sessionExpired', 'csrfRejected', 'projectArchived', 'forbidden', 'notFound'] as const)(
    'passes typed Workspace %s through the result owner without interpreting its message', (key) => {
      const html = renderToStaticMarkup(<RunResultPanel actorId={INTERACTION_SCOPE.actorId} projectId={INTERACTION_SCOPE.projectId}
        runId={INTERACTION_SCOPE.runId} csrfToken="synthetic-session" state={{ status: 'error', detail: interactionDetail(),
          message: 'Unrelated localized status', accessFailure: { key } }} />)
      expect(html).toContain('Which public option?')
      expect(html).toContain(MESSAGES.zh.interactionResponse.failures[key])
      expect(html.match(/data-interaction-card=/g)).toHaveLength(1)
      expect(html).not.toContain('data-interaction-confirm-original')
      expect(html).toContain(MESSAGES.zh.interactionResponse.recordsTitle)
      expect(html).not.toContain(MESSAGES.zh.runResult.pendingHint)
    },
  )
})
