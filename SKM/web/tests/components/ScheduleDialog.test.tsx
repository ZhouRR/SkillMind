import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ScheduleDialog, ScheduleEditDialog, ScheduleStatusActions } from '../../src/components/ScheduleDialog'
import { SourceRequirementField } from '../../src/components/TaskLaunchFields'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { documentTask } from '../fixtures/documentTask'
import { scheduleFixture } from '../fixtures/schedule'

describe.each(['zh', 'ja', 'en'] as const)('schedule confirmation in %s', (language) => {
  it('retains archive but omits resume when scheduled execution is disabled', () => {
    const schedule = scheduleFixture({ status: 'PAUSED' })
    const html = renderToStaticMarkup(<LanguageProvider language={language}><ScheduleStatusActions
      schedule={schedule} projectId={schedule.project_id} csrfToken="fixture" allowResume={false}
      onChanged={() => {}} onError={() => {}} /></LanguageProvider>)
    expect(html).not.toContain('data-schedule-status="ACTIVE"')
    expect(html).toContain('data-schedule-status="ARCHIVED"')
    expect(html).not.toContain('data-schedule-status="ARCHIVED" disabled')
  })

  it('names the browser input zone independently of the rule zone', () => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}><ScheduleDialog open
      projectId="00000000-0000-4000-8000-000000000020" csrfToken="fixture"
      task={documentTask()} onClose={() => {}} onSaved={() => {}} /></LanguageProvider>)
    expect(html).toContain(MESSAGES[language].schedules.ruleTimezoneHint)
    expect(html).toContain(MESSAGES[language].schedules.inputTimezone(Intl.DateTimeFormat().resolvedOptions().timeZone))
    expect(html).toContain('data-schedule-input-timezone')
    expect(html).toContain('type="submit" disabled=""')
    expect(html).not.toContain('data-schedule-confirm')
    expect(html).toContain(MESSAGES[language].schedules.closingHint)
  })

  it('initializes the shared editor from original values without confirming a preview', () => {
    const task = documentTask()
    const schedule = scheduleFixture({ skill_version_id: task.skill_version_id, task_key: task.task_key,
      end_at: '2027-11-07T06:30:32.123456Z' })
    const html = renderToStaticMarkup(<LanguageProvider language={language}><ScheduleEditDialog open
      projectId={schedule.project_id} csrfToken="fixture" schedule={schedule}
      task={task} onClose={() => {}} onSaved={() => {}} /></LanguageProvider>)
    expect(html).toContain(MESSAGES[language].scheduleEditor.title)
    expect(html).toContain('value="Original schedule"')
    expect(html).toContain(String(schedule.input.objective))
    expect(html).toContain(schedule.end_at)
    expect(html).toContain('data-schedule-editor')
    expect(html).not.toContain('data-schedule-confirm')
    expect(html).toContain('type="submit" disabled=""')
  })

  it('retains an unavailable original source instead of displaying a new sole choice as selected', () => {
    const requirement = { key: 'issues', kind: 'issue', access: 'read', required: true,
      options: [{ value: 'integration:new', label: 'New candidate' }] }
    const html = renderToStaticMarkup(<LanguageProvider language={language}><SourceRequirementField
      requirement={requirement} value="integration:original" onChange={() => {}} /></LanguageProvider>)
    expect(html).toContain('value="integration:original" disabled="" selected=""')
    expect(html).toContain(MESSAGES[language].scheduleEditor.sourceUnavailable)
    expect(html).toContain('value="integration:new"')
    expect(html).not.toContain(MESSAGES[language].workspace.willUseSource('New candidate'))
  })

  it('shows an explicit source selector even when its retained source has no current candidates', () => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}><SourceRequirementField
      requirement={{ key: 'issues', kind: 'issue', access: 'read', required: true, options: [] }}
      value="integration:original" onChange={() => {}} /></LanguageProvider>)
    expect(html).toContain('value="integration:original" disabled="" selected=""')
  })

  it('keeps state controls disabled for a readonly project', () => {
    const schedule = scheduleFixture()
    const html = renderToStaticMarkup(<LanguageProvider language={language}><ScheduleStatusActions
      schedule={schedule} projectId={schedule.project_id} csrfToken="fixture" disabled
      onChanged={() => {}} onError={() => {}} /></LanguageProvider>)
    expect(html).toContain('data-schedule-status="PAUSED" disabled=""')
    expect(html).toContain('data-schedule-status="ARCHIVED" disabled=""')
  })

  it('keeps completed original editing readonly while retaining all fields', () => {
    const task = documentTask()
    const schedule = scheduleFixture({ status: 'COMPLETED', skill_version_id: task.skill_version_id, task_key: task.task_key })
    const html = renderToStaticMarkup(<LanguageProvider language={language}><ScheduleEditDialog open
      projectId={schedule.project_id} csrfToken="fixture" schedule={schedule}
      task={task} onClose={() => {}} onSaved={() => {}} /></LanguageProvider>)
    expect(html).toContain('<fieldset disabled="" class="scheduleConfiguration">')
    expect(html).toContain(MESSAGES[language].scheduleEditor.unavailable)
    expect(html).toContain('value="Original schedule"')
  })
})
