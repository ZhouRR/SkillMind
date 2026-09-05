import { describe, expect, it } from 'vitest'

import type { RunEventRecord } from '../../src/api'
import { applicableRunSnapshot } from '../../src/lib/runReplay'

/** Replay rule test 用の RUN_SNAPSHOT event を生成する。 */
function snapshot(status: string, rowVersion: number): RunEventRecord {
  return {
    run_id: '00000000-0000-4000-8000-000000000001',
    run_attempt_id: null,
    agent_session_id: null,
    sequence: rowVersion,
    event_type: 'RUN_SNAPSHOT',
    occurred_at: '2026-07-02T13:00:00Z',
    payload: { status, row_version: rowVersion },
    trace_id: null,
  }
}

describe('terminal Run replay', () => {
  it('ignores historical snapshots older than the selected terminal Run', () => {
    /** History 再表示時に QUEUED へ巻き戻らないことを守る。 */
    expect(applicableRunSnapshot(snapshot('QUEUED', 1), 4)).toBeNull()
  })

  it('accepts the terminal snapshot at the current row version', () => {
    /** Persisted replay の最後で terminal identity を確定できることを守る。 */
    expect(applicableRunSnapshot(snapshot('SUCCEEDED', 4), 4)).toEqual({
      status: 'SUCCEEDED',
      rowVersion: 4,
    })
  })
})
