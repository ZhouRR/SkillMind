import { isValidElement, type ReactElement, type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ScheduleEditDialog, ScheduleStatusActions } from '../../src/components/ScheduleDialog'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { SchedulesPage, type SchedulesPageProps } from '../../src/pages/SchedulesPage'
import { DEMO_PROJECT, demoUser } from '../fixtures'
import { documentTask } from '../fixtures/documentTask'
import { scheduleFixture } from '../fixtures/schedule'

/** DOM の前に原 callback を同 tick で実行し、render 前の owner 排他を検査する。 */
const phases = vi.hoisted(() => ({ cursor: 0, slots: [] as unknown[] }))
vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useState: (initial: unknown) => {
    const index = phases.cursor++
    if (!(index in phases.slots)) phases.slots[index] = initial
    return [phases.slots[index], (next: unknown) => {
      phases.slots[index] = typeof next === 'function' ? next(phases.slots[index]) : next
    }]
  },
  useRef: (initial: unknown) => {
    const index = phases.cursor++
    return phases.slots[index] ?? (phases.slots[index] = { current: initial })
  },
  useEffect: () => {},
}))
vi.mock('../../src/i18n', () => ({ useMessages: () => MESSAGES.en }))
vi.mock('../../src/hooks/useSchedules', () => ({ useSchedules: () => {
  const record = scheduleFixture()
  const query = { data: record, pending: false, failure: null, refresh: vi.fn() }
  return {
    selection: { id: record.schedule_id, revision: 1 }, record, task: documentTask(),
    taskEligibility: 'ready', readDenied: null, canWrite: true, canEdit: () => true,
    detail: query, catalog: query, activity: { ...query, data: null }, list: { ...query, data: { schedules: [record], total: 1, limit: 25, offset: 0 } },
    filter: { q: '', status: '', offset: 0 }, limit: 25,
  }
} }))

/** 子 component は呼び出さず、親が共有 request owner へ渡した契約だけを観測する。 */
type Node = ReactElement<Record<string, unknown>>
function find(node: ReactNode, predicate: (value: Node) => boolean): Node {
  if (isValidElement<Record<string, unknown>>(node)) {
    if (predicate(node)) return node
    for (const child of [node.props.children].flat(Infinity)) {
      try { return find(child as ReactNode, predicate) } catch { /* 次の sibling を調べる。 */ }
    }
  }
  throw new Error('Expected parent element is missing')
}
/** React の外部 mount は actor/Project key を既に持つ。内側 owner を繰返し描画する。 */
function render(): ReactNode {
  phases.cursor = 0
  const props = { projectId: DEMO_PROJECT.project_id, currentProject: DEMO_PROJECT,
    actorId: demoUser().user_id, csrfToken: 'synthetic-token' }
  const outer = SchedulesPage(props)
  return (outer.type as (props: SchedulesPageProps) => ReactNode)(props)
}
/** Props の型 escape は汎用 tree 調査の境界だけに閉じ込める。 */
function invoke(node: Node, name: string, ...args: unknown[]): unknown {
  return (node.props[name] as (...values: unknown[]) => unknown)(...args)
}
beforeEach(() => { phases.cursor = 0; phases.slots = [] })

describe('schedule manager synchronous writer ownership', () => {
  it('preserves an accepted editor through same-tick close and an old edit callback', () => {
    let tree = render()
    const edit = find(tree, (node) => 'data-schedule-edit' in node.props)
    invoke(edit, 'onClick')
    tree = render()
    const original = find(tree, (node) => node.type === ScheduleEditDialog)
    const oldEdit = find(tree, (node) => 'data-schedule-edit' in node.props)
    invoke(original, 'onPendingChange', true)
    invoke(original, 'onClose')
    invoke(oldEdit, 'onClick')
    tree = render()
    const preserved = find(tree, (node) => node.type === ScheduleEditDialog)
    expect(preserved.key).toBe(original.key)
    expect(preserved.props.open).toBe(false)
    expect(find(tree, (node) => 'data-schedule-reopen' in node.props)).toBeTruthy()
  })
  it('closes the old status callback immediately when the editor opens', () => {
    const tree = render()
    const status = find(tree, (node) => node.type === ScheduleStatusActions)
    expect(invoke(status, 'isWriteAllowed')).toBe(true)
    invoke(find(tree, (node) => 'data-schedule-edit' in node.props), 'onClick')
    expect(invoke(status, 'isWriteAllowed')).toBe(false)
  })
  it('closes the old edit callback immediately after status accepts without self-locking status', () => {
    let tree = render()
    const status = find(tree, (node) => node.type === ScheduleStatusActions)
    invoke(status, 'onPendingChange', true)
    invoke(find(tree, (node) => 'data-schedule-edit' in node.props), 'onClick')
    expect(invoke(status, 'isWriteAllowed')).toBe(true)
    tree = render()
    expect(() => find(tree, (node) => node.type === ScheduleEditDialog)).toThrow()
  })
})
