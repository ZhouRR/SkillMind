import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { LazyAuditSection } from '../../src/components/LazyAuditSection'

describe('lazy audit section', () => {
  it('shows the title and count without evaluating the closed body', () => {
    /** 既定の折り畳みが stringify/map を実行しないことを検証する。 */
    const body = vi.fn(() => { throw new Error('Closed content must not be evaluated') })
    const html = renderToStaticMarkup(<LazyAuditSection title="Audit" count={50}>{body}</LazyAuditSection>)
    expect(html).toContain('Audit')
    expect(html).toContain('50')
    expect(html).not.toContain('resultCollapseBody')
    expect(body).not.toHaveBeenCalled()
  })
})
