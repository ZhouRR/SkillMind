import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunEventRecord } from '../../src/api'
import { createAgentStreamProjector, projectAgentStream } from '../../src/lib/agentStream'
import { appendOrderedEvents, createAuditEventProjector, createEventBatcher, hasAppendOnlyPrefix } from '../../src/lib/runEventBuffer'

/** 本文・監査・順序を照合する synthetic event。 */
function event(sequence: number, event_type = 'TEXT_DELTA', text = 'x'): RunEventRecord {
  return { run_id: 'run', run_attempt_id: null, agent_session_id: null, sequence,
    event_type, occurred_at: '2026-01-01T00:00:00Z', payload: { text }, trace_id: null }
}
afterEach(() => vi.useRealTimers())

describe('ordered event batches', () => {
  it('preserves ordering and first-wins duplicates within and between batches', () => {
    const original = [event(2), event(4)]
    const first = event(3)
    const result = appendOrderedEvents(original, [event(5), first, event(1), event(3, 'TEXT_COMPLETED'), event(2)])
    expect(result.map((item) => item.sequence)).toEqual([1, 2, 3, 4, 5])
    expect(result[1]).toBe(original[0])
    expect(result[2]).toBe(first)
    expect(original.map((item) => item.sequence)).toEqual([2, 4])
    expect(appendOrderedEvents(original, [event(4), event(2)])).toBe(original)
  })
  it('certifies skipped append batches but rejects branches and insertion', () => {
    const root: RunEventRecord[] = []
    const first = appendOrderedEvents(root, [event(1)])
    const second = appendOrderedEvents(first, [event(3)])
    const third = appendOrderedEvents(second, [event(4)])
    expect(hasAppendOnlyPrefix(first, third)).toBe(true)
    expect(hasAppendOnlyPrefix(second, appendOrderedEvents(first, [event(5)]))).toBe(false)
    expect(hasAppendOnlyPrefix(second, appendOrderedEvents(second, [event(2)]))).toBe(false)
    expect(hasAppendOnlyPrefix(third, first)).toBe(false)
    expect(hasAppendOnlyPrefix(first, [...third])).toBe(false)
  })
  it('rebuilds both projections on divergent equal-length appends', () => {
    const root: RunEventRecord[] = []
    const base = appendOrderedEvents(root, [event(1)])
    const left = appendOrderedEvents(base, [event(2, 'TEXT_COMPLETED', 'left')])
    const right = appendOrderedEvents(base, [event(2, 'TEXT_COMPLETED', 'right')])
    const project = createAgentStreamProjector()
    const audit = createAuditEventProjector<RunEventRecord>()
    for (const branch of [left, right, left]) {
      expect(project(branch)).toEqual(projectAgentStream(branch))
      expect(audit(branch)).toEqual(branch.filter((item) => item.event_type !== 'TEXT_DELTA'))
    }
  })
  it('keeps full text, structured results and durable audit equivalent through replay', () => {
    let events: RunEventRecord[] = []
    const project = createAgentStreamProjector()
    const audit = createAuditEventProjector<RunEventRecord>()
    for (const batch of [[event(1), event(2)], [event(3, 'TEXT_COMPLETED', 'xx')],
      [event(5, 'TEXT_DELTA', '{')], [event(6, 'TEXT_COMPLETED', '{"result":1}')],
      [event(4, 'SESSION_STARTED')], [event(3), event(7, 'RUN_SNAPSHOT')]]) {
      events = appendOrderedEvents(events, batch)
      expect(project(events)).toEqual(projectAgentStream(events))
      expect(audit(events)).toEqual(events.filter((item) => item.event_type !== 'TEXT_DELTA'))
    }
    const oldAudit = audit(events)
    events = appendOrderedEvents(events, [event(8)])
    expect(audit(events)).toBe(oldAudit)
    expect(project(events)).toEqual(projectAgentStream(events))
    expect(project([])).toEqual(projectAgentStream([]))
    expect(audit([])).toEqual([])
  })
})

describe('stream batching lifecycle', () => {
  it('flushes without requestAnimationFrame in a hidden tab', () => {
    vi.useFakeTimers()
    const commit = vi.fn()
    const batcher = createEventBatcher<RunEventRecord>(commit)
    batcher.push(event(1)); batcher.push(event(2))
    expect(commit).not.toHaveBeenCalled()
    vi.advanceTimersByTime(16)
    expect(commit).toHaveBeenCalledExactlyOnceWith([event(1), event(2)])
    expect(vi.getTimerCount()).toBe(0)
  })
  it('flushes complete text and terminal snapshot before cleanup', () => {
    vi.useFakeTimers()
    const commit = vi.fn()
    const batcher = createEventBatcher<RunEventRecord>(commit)
    batcher.push(event(1)); batcher.push(event(2, 'TEXT_COMPLETED', 'complete')); batcher.push(event(3, 'RUN_SNAPSHOT'))
    batcher.flush(); batcher.dispose(); vi.runAllTimers()
    expect(commit).toHaveBeenCalledTimes(1)
    expect(projectAgentStream(commit.mock.calls[0]![0]).completed).toEqual([
      { sequence: 2, kind: 'text', text: 'complete' },
    ])
    expect(vi.getTimerCount()).toBe(0)
  })
  it('rejects old-run events after cleanup while new run remains independent', () => {
    vi.useFakeTimers()
    const oldCommit = vi.fn(); const currentCommit = vi.fn()
    const old = createEventBatcher<RunEventRecord>(oldCommit)
    old.push(event(1)); old.dispose(); old.push(event(2))
    const current = createEventBatcher<RunEventRecord>(currentCommit)
    current.push(event(3)); vi.runAllTimers()
    expect(oldCommit).not.toHaveBeenCalled()
    expect(currentCommit).toHaveBeenCalledExactlyOnceWith([event(3)])
  })
  it.each([10000, 50000])('bounds commits and copies for a %i-event burst', (count) => {
    vi.useFakeTimers()
    let events: RunEventRecord[] = []; let commits = 0; let copiedEntries = 0
    const project = createAgentStreamProjector()
    const audit = createAuditEventProjector<RunEventRecord>()
    const batcher = createEventBatcher<RunEventRecord>((batch) => {
      copiedEntries += events.length
      events = appendOrderedEvents(events, batch); commits += 1
      expect(audit(events)).toEqual([])
      expect(project(events).partial.length).toBe(events.length)
    })
    for (let sequence = 1; sequence <= count; sequence += 1) batcher.push(event(sequence))
    batcher.flush()
    expect(commits).toBe(Math.ceil(count / 256))
    expect(events.length).toBe(count)
    expect(project(events).partial).toBe('x'.repeat(count))
    // 元の 1 event/更新では履歴 copy が N(N-1)/2。実 copy 入力件数を比較する。
    expect(copiedEntries).toBe(256 * commits * (commits - 1) / 2)
    expect(copiedEntries).toBeLessThan(count * (count - 1) / 2 / 200)
    expect(vi.getTimerCount()).toBe(0)
  })
})
