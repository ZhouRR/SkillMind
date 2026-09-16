import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { runDuration } from '../../src/lib/runDuration'
import { RunDuration } from '../../src/components/RunDuration'

it('uses start and finish rather than creation, retaining response waits and duration over an hour', () => {
  const run = { status: 'SUCCEEDED', created_at: '2026-09-16T00:00:00Z', started_at: '2026-09-16T01:00:00Z', finished_at: '2026-09-16T02:09:31Z' }
  expect(runDuration(run, 0)).toEqual({ kind: 'finished', seconds: 4171 })
  expect(renderToStaticMarkup(createElement(RunDuration, { run }))).toContain('1小时 9分 31秒')
})

it('shows elapsed for active and waiting-for-approval execution, without measuring the queue', () => {
  const now = Date.parse('2026-09-16T01:09:31Z')
  expect(runDuration({ status: 'QUEUED', started_at: null }, now)).toEqual({ kind: 'waiting' })
  expect(runDuration({ status: 'WAITING_FOR_APPROVAL', started_at: '2026-09-16T01:00:00Z' }, now)).toEqual({ kind: 'elapsed', seconds: 571 })
  expect(runDuration({ status: 'FAILED', started_at: '2026-09-16T01:00:00Z' }, now)).toEqual({ kind: 'unknown' })
  expect(runDuration({ status: 'SUCCEEDED', started_at: 'invalid' }, now)).toEqual({ kind: 'unknown' })
  expect(runDuration({ status: 'RUNNING', started_at: '2026-09-17T00:00:00Z' }, now)).toEqual({ kind: 'unknown' })
})
