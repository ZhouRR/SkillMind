import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ReportPresentation } from '../../src/components/ReportPresentation'

it('falls back to the unchanged readable result, escaping markup without another action', () => {
  const value = { summary: '<script>private()</script>', limitations: ['Not verified'] }
  const snapshot = JSON.stringify(value)
  const boundary = new ReportPresentation({ value, children: <div>Template</div> })
  boundary.state = ReportPresentation.getDerivedStateFromError()
  const html = renderToStaticMarkup(boundary.render())
  expect(html).toContain('Not verified')
  expect(html).toContain('&lt;script&gt;')
  expect(html).not.toContain('<script>')
  expect(JSON.stringify(value)).toBe(snapshot)
})
