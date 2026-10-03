import type { ReactElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { StaticPreviewFrame } from '../../src/components/StaticPreviewFrame'
import { commitHooks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)

/** iframe の native load を合成し、表示 owner と sandbox 属性だけを検証する。 */
interface FrameProps { srcDoc: string; sandbox: string; referrerPolicy: string;
  style: { visibility: string }; onLoad: () => void }
function render(source: string) {
  hookPhases.cursor = 0
  const result = StaticPreviewFrame({ source, title: 'Preview' }) as ReactElement<{
    'aria-busy': boolean; children: ReactElement<FrameProps>
  }>
  commitHooks()
  return { busy: result.props['aria-busy'], frame: result.props.children }
}
afterEach(unmountHooks)

describe('static iframe loading surface', () => {
  it('keeps the initial canvas hidden until native load while retaining its sandbox', () => {
    const first = render('<html>first</html>')
    expect(first.busy).toBe(true)
    expect(first.frame.props.style.visibility).toBe('hidden')
    expect(first.frame.props.sandbox).toBe('')
    expect(first.frame.props.referrerPolicy).toBe('no-referrer')
    first.frame.props.onLoad()
    expect(render('<html>first</html>').frame.props.style.visibility).toBe('visible')
  })
  it('hides a new document and never accepts the previous document load as ready', () => {
    const first = render('first'); first.frame.props.onLoad()
    const second = render('second')
    expect(second.frame.key).not.toBe(first.frame.key)
    first.frame.props.onLoad()
    expect(render('second').frame.props.style.visibility).toBe('hidden')
    second.frame.props.onLoad()
    expect(render('second').frame.props.style.visibility).toBe('visible')
    first.frame.props.onLoad()
    expect(render('second').frame.props.style.visibility).toBe('visible')
  })
})
