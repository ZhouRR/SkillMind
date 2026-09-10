import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { ScheduleActivityPanel, type ScheduleActivityPanelProps } from '../../src/components/ScheduleActivityPanel'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, type UiLanguage } from '../../src/lib/i18n/messages'
import { scheduleActivityFixture } from '../fixtures/scheduleActivity'

/** 読取済みの公開 fixture を投影し、SSR と実 browser の証明範囲を混同しない。 */
function render(language: UiLanguage, props: Partial<ScheduleActivityPanelProps> = {}): string {
  return renderToStaticMarkup(<LanguageProvider language={language}><ScheduleActivityPanel
    data={scheduleActivityFixture()} pending={false} failure={null} onRefresh={vi.fn()} {...props} /></LanguageProvider>)
}

describe.each(['zh', 'ja', 'en'] as const)('read-only activity in %s', (language) => {
  const labels = MESSAGES[language].scheduleActivity
  it('shows original and current configuration separately with the server check timestamp', () => {
    const html = render(language)
    expect(html).toContain(labels.title)
    expect(html).toContain(labels.originalConfiguration)
    expect(html).toContain(labels.currentConfiguration)
    expect(html).toContain(labels.configurationHint)
    expect(html).toContain(labels.scopeHint)
    expect(html).toContain('2035-01-01T12:05:00Z')
    expect(html).toContain('data-schedule-pending="true"')
    expect(html).toContain('data-schedule-lease="expired"')
    expect(html).toContain('data-schedule-attempt-limit="reached"')
    expect(html).toContain(labels.leaseLimit)
    expect(html).toContain(labels.attemptsReached)
    expect(html.match(/<button/g)).toHaveLength(1)
    expect(html).not.toContain('type="submit"')
  })
  it('does not interpret legacy unavailability as an empty tracked history', () => {
    const html = render(language, { data: scheduleActivityFixture({ tracking: 'LEGACY_UNAVAILABLE', pending: null }) })
    expect(html).toContain('data-schedule-activity-tracking="LEGACY_UNAVAILABLE"')
    expect(html).toContain(labels.legacy)
    expect(html).not.toContain(labels.empty)
    expect(html).not.toContain('data-schedule-pending=')
  })
  it('limits tracked emptiness to the current read without proving a safe replay', () => {
    const html = render(language, { data: scheduleActivityFixture({ pending: null }) })
    expect(html).toContain('data-schedule-activity-tracking="TRACKED"')
    expect(html).toContain(labels.empty)
    expect(html).toContain(labels.emptyLimit)
    expect(html).not.toContain(labels.legacy)
  })
  it('does not present old pending facts as a successful current read while loading', () => {
    const html = render(language, { pending: true })
    expect(html).toContain(labels.loading)
    expect(html).toContain('data-schedule-activity-refresh="true" disabled=""')
    expect(html).not.toContain('data-schedule-pending=')
    expect(html).not.toContain('data-schedule-activity-tracking=')
  })
  it.each(['loadFailed', 'accessUnavailable', 'sessionExpired'] as const)('shows %s without fabricating empty or keeping stale lease claims', (key) => {
    const html = render(language, { failure: { key } })
    expect(html).toContain(MESSAGES[language].scheduleManager.failures[key])
    expect(html).toContain('data-schedule-activity-error="true"')
    expect(html).not.toContain(labels.empty)
    expect(html).not.toContain('data-schedule-lease=')
  })
})

describe('lease snapshot precision', () => {
  it.each([
    ['2035-01-01T21:04:00.000001+09:00', '2035-01-01T12:04:00.000002Z', 'active'],
    ['2035-01-01T21:04:00.000002+09:00', '2035-01-01T12:04:00.000002Z', 'expired'],
    ['2035-01-01T21:04:00.000003+09:00', '2035-01-01T12:04:00.000002Z', 'expired'],
  ])('compares checked_at=%s and expiry=%s without millisecond rounding', (checked, expiry, state) => {
    const original = scheduleActivityFixture()
    const html = render('en', { data: { ...original, checked_at: checked,
      pending: { ...original.pending!, lease_expires_at: expiry, attempt_count: 1 } } })
    expect(html).toContain(`data-schedule-lease="${state}"`)
    expect(html).toContain(checked)
    expect(html).toContain(expiry)
    expect(html).toContain('data-schedule-attempt-limit="remaining"')
  })
  it('uses checked_at rather than the browser wall clock for an unexpired lease', () => {
    const original = scheduleActivityFixture()
    const html = render('en', { data: { ...original, checked_at: '2001-01-01T00:00:00Z',
      pending: { ...original.pending!, lease_expires_at: '2001-01-01T00:01:00Z' } } })
    expect(html).toContain('data-schedule-lease="active"')
  })
})
