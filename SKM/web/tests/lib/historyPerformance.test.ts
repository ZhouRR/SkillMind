import { describe, expect, it, vi } from 'vitest'

import type { RunEventRecord } from '../../src/api'
import { createAgentStreamProjector, projectAgentStream } from '../../src/lib/agentStream'
import { appendOrderedEvent } from '../../src/lib/runEventBuffer'

/** 新旧投影を同じ完全 event shape で比較する。 */
function event(sequence: number, eventType = 'TEXT_DELTA', text = String(sequence)): RunEventRecord {
  return {
    run_id: '00000000-0000-4000-8000-000000000001',
    run_attempt_id: '00000000-0000-4000-8000-000000000002',
    agent_session_id: '00000000-0000-4000-8000-000000000003',
    sequence, event_type: eventType, payload: { text },
    occurred_at: '2026-09-16T00:00:00Z', trace_id: null,
  }
}

describe('ordered event insertion', () => {
  it('keeps replay first-wins without mutating old arrays', () => {
    const first = event(10)
    const initial = [first]
    const appended = appendOrderedEvent(initial, event(30))
    expect(initial).toEqual([first])
    expect(appendOrderedEvent(appended, event(10, 'TEXT_DELTA', 'replacement'))).toBe(appended)
    expect(appendOrderedEvent(appended, event(20)).map((value) => value.sequence)).toEqual([10, 20, 30])
    expect(appendOrderedEvent(appended, event(1)).map((value) => value.sequence)).toEqual([1, 10, 30])
  })

  it('matches the old insertion behavior over shuffled replay', () => {
    let current: RunEventRecord[] = []
    let expected: RunEventRecord[] = []
    for (const sequence of [9, 2, 6, 1, 9, 5, 8, 2, 7, 3, 4]) {
      const incoming = event(sequence)
      current = appendOrderedEvent(current, incoming)
      if (!expected.some((value) => value.sequence === sequence)) {
        expected = [...expected, incoming].sort((a, b) => a.sequence - b.sequence)
      }
      expect(current).toEqual(expected)
    }
  })
})

describe('incremental stream projection', () => {
  it('matches the original view through completion, replay, replacement and reset', () => {
    const project = createAgentStreamProjector()
    let events: RunEventRecord[] = []
    for (const incoming of [event(2), event(4), event(7, 'TEXT_COMPLETED', '{"items":[1,2]}'), event(9)]) {
      events = appendOrderedEvent(events, incoming)
      expect(project(events)).toEqual(projectAgentStream(events))
    }
    events = appendOrderedEvent(events, event(3))
    expect(project(events)).toEqual(projectAgentStream(events))
    const replaced = [...events]
    replaced[1] = event(3, 'TEXT_DELTA', 'corrected')
    expect(project(replaced)).toEqual(projectAgentStream(replaced))
    expect(project([])).toEqual(projectAgentStream([]))
    expect(project([event(1)])).toEqual(projectAgentStream([event(1)]))
  })

  it('handles unsorted input and retains previously returned views', () => {
    const project = createAgentStreamProjector()
    const old = [event(3), event(1)]
    const view = project(old)
    const serialized = JSON.stringify(view)
    const next = [...old, event(4, 'TEXT_COMPLETED', 'done')]
    expect(project(next)).toEqual(projectAgentStream(next))
    expect(JSON.stringify(view)).toBe(serialized)
    expect(project(next)).toBe(project(next))
  })

  it('does not reparse a finished JSON message on every later delta', () => {
    const project = createAgentStreamProjector()
    const parse = vi.spyOn(JSON, 'parse')
    try {
      let events = [event(1, 'TEXT_COMPLETED', '{"summary":"ok"}')]
      project(events)
      for (let sequence = 2; sequence <= 1000; sequence += 1) {
        events = appendOrderedEvent(events, event(sequence))
        project(events)
      }
      expect(parse).toHaveBeenCalledTimes(1)
    } finally {
      parse.mockRestore()
    }
  })

  it('handles fenced JSON and non-text audit events without altering semantics', () => {
    const project = createAgentStreamProjector()
    const events = [event(1, 'TEXT_DELTA', '```json\n{'),
      event(2, 'TOOL_COMPLETED', ''), event(3, 'TEXT_COMPLETED', '```json\n{"x":1}\n```')]
    for (let count = 0; count <= events.length; count += 1) {
      expect(project(events.slice(0, count))).toEqual(projectAgentStream(events.slice(0, count)))
    }
  })
})
