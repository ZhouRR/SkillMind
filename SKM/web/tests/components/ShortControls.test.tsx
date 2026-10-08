// @vitest-environment jsdom
import { type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { DocumentManagerPanel } from '../../src/components/DocumentManagerPanel'
import { ResourceConnectionForm } from '../../src/components/ResourceConnectionForm'
import { ScheduleDialog } from '../../src/components/ScheduleDialog'
import { ScheduleRecurrenceField } from '../../src/components/ScheduleRecurrenceField'
import { SchemaTaskInput } from '../../src/components/SchemaTaskInput'
import { LanguageProvider } from '../../src/i18n'
import { UI_LANGUAGES, type UiLanguage } from '../../src/lib/i18n/messages'
import { emptyConnectDraft } from '../../src/lib/resourceDrafts'
import { TasksPage } from '../../src/pages/TasksPage'
import { documentTask } from '../fixtures/documentTask'

/** API を呼ばない SSR で実消費側の短い field 指定を検証する。画素検証は browser fixture が担う。 */
function render(node: ReactNode, language: UiLanguage): HTMLDivElement {
  const container = document.createElement('div')
  container.innerHTML = renderToStaticMarkup(<LanguageProvider language={language}>{node}</LanguageProvider>)
  return container
}

/** 短い項目だけが popup と field の両方を明示指定し、通常 trigger の契約を維持する。 */
function expectShort(element: Element | null, narrow = false): void {
  expect(element).not.toBeNull()
  expect(element!.getAttribute('data-density')).toBe('compact')
  expect(element!.classList.contains('shortControl')).toBe(true)
  expect(element!.classList.contains('shortControlNarrow')).toBe(narrow)
  expect(element!.getAttribute('role')).toBe('combobox')
}

describe.each(UI_LANGUAGES)('short control consumers in %s', (language) => {
  it('compacts document sorting and task status without shrinking the search field', () => {
    const documents = render(<DocumentManagerPanel projectId="fixture-project" csrfToken="fixture"
      actorId="fixture-actor" readOnly={false} onSessionEnded={() => {}} />, language)
    expectShort(documents.querySelector('.documentSort.shortField .selectTrigger'), true)
    expect(documents.querySelector('.documentManagementToolbar > input')!.className).not.toContain('short')
    const tasks = render(<TasksPage projectId="fixture-project" moduleId="" csrfToken="fixture" />, language)
    expectShort(tasks.querySelector('.taskFilters .shortField [data-task-status]'))
    expect(tasks.querySelector('[data-task-search]')!.className).not.toContain('short')
  })

  it('compacts booleans but keeps arbitrary enum labels and numeric schemas unconstrained', () => {
    const view = render(<SchemaTaskInput schema={{ type: 'object', properties: {
      enabled: { type: 'boolean', title: 'Boolean field' },
      project: { type: 'string', enum: ['A very long project or resource name', 'Other'], title: 'Long enum' },
      count: { type: 'number', title: 'Schema number' },
    } }} rawValue='{"enabled":true,"project":"A very long project or resource name"}' onRawChange={() => {}} value={{ enabled: true, project: 'A very long project or resource name' }} onChange={() => {}} />, language)
    const selects = view.querySelectorAll('.selectTrigger')
    expectShort(selects[0]!, true)
    expect(selects[1]!.getAttribute('data-density')).toBe('default')
    expect(selects[1]!.className).not.toContain('shortControl')
    expect(selects[1]!.textContent).toContain('A very long project or resource name')
    expect(view.querySelector('input[type=number]')!.className).not.toContain('shortNumberControl')
  })

  it('compacts recurrence choices while keeping time and custom cron fields full sized', () => {
    const weekly = render(<ScheduleRecurrenceField value="30 9 * * 1" onChange={() => {}} />, language)
    expectShort(weekly.querySelector('[data-field-name=recurrence]'))
    expectShort(weekly.querySelector('.shortField [data-field-name=recurrence_weekday]'))
    expect(weekly.querySelector('input[type=time]')!.className).not.toContain('short')
    const custom = render(<ScheduleRecurrenceField value="*/5 * * * *" onChange={() => {}} />, language)
    expect(custom.querySelector('[name=cron_expression]')!.className).not.toContain('short')
  })

  it('compacts schedule kind and run limit without narrowing timezone or dates', () => {
    const view = render(<ScheduleDialog open projectId="fixture-project" csrfToken="fixture"
      task={documentTask()} onClose={() => {}} onSaved={() => {}} />, language)
    expectShort(view.querySelector('[data-field-name=kind]'))
    expect(view.querySelector('[name=max_runs]')!.classList.contains('shortNumberControl')).toBe(true)
    expect(view.querySelector('[name=max_runs]')!.getAttribute('max')).toBe('100000')
    expect(view.querySelector('[name=timezone]')!.className).not.toContain('short')
    expect(view.querySelector('input[type=datetime-local]')!.className).not.toContain('short')
  })

  it('compacts only the database port in the resource form', () => {
    const view = render(<ResourceConnectionForm connectDraft={emptyConnectDraft('postgres')} setConnectDraft={() => {}}
      secrets={[]} editingIntegration={null} connectWriteEnabled={false} mcpToolsEnabled={false}
      busy={null} error={null} submitConnect={async () => {}} discoverTools={async () => {}}
      locked={false} feedback={null} onClose={() => {}} />, language)
    const port = view.querySelector('input[type=number]')!
    expect(port.classList.contains('shortNumberControl')).toBe(true)
    expect(port.getAttribute('min')).toBe('1')
    expect(port.getAttribute('max')).toBe('65535')
    expect(view.querySelectorAll('.selectTrigger[data-density=compact]')).toHaveLength(0)
    expect(view.querySelectorAll('input.shortNumberControl')).toHaveLength(1)
  })
})
