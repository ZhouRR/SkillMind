import { afterEach, describe, expect, it, vi } from 'vitest'

import { subscribeInterpretEvents } from '../../src/api/skills'

const ID = '00000000-0000-4000-8000-000000000061'
const KEY = `sha256:${'a'.repeat(64)}`

/** 外部 SSE を使わず、購読 channel と停止動作を観測する。 */
class Source extends EventTarget {
  static current: Source
  onerror: (() => void) | null = null
  close = vi.fn()

  constructor(readonly url: string) {
    super()
    Source.current = this
  }
}

afterEach(() => vi.unstubAllGlobals())

/** 通知は原要求に束縛され、壊れた frame/断連から暗黙再接続しない。 */
describe('interpretation event subscription', () => {
  it.each(['wrong-key', 'wrong-event', 'broken-json', 'disconnect'])('closes on %s', (mode) => {
    vi.stubGlobal('EventSource', Source)
    const onEvent = vi.fn()
    const onError = vi.fn()
    subscribeInterpretEvents(ID, KEY, onEvent, onError)
    const source = Source.current
    expect(source.url).toMatch(new RegExp(`/skill-interpretation-requests/${ID}/events$`))
    if (mode === 'disconnect') source.onerror?.()
    else source.dispatchEvent(new MessageEvent('interpret.delta', {
      data: mode === 'broken-json' ? '{' : JSON.stringify({
        event: mode === 'wrong-event' ? 'interpret.completed' : 'interpret.delta',
        execution_key: mode === 'wrong-key' ? `sha256:${'b'.repeat(64)}` : KEY,
        occurred_at: '2026-09-11T00:00:00Z', data: {},
      }),
    }))
    expect(onEvent).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledOnce()
    expect(source.close).toHaveBeenCalledOnce()
  })
})
