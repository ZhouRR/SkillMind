import { describe, expect, it } from 'vitest'
import css from '../../src/styles/workspace-audit.css?raw'

/** 宣言の保護だけを検証し、実 browser の幅・色・focus の視覚確認とは区別する。 */
describe('Workspace reading style contracts', () => {
  it('keeps long recent-run titles fully readable instead of ellipsized', () => {
    expect(css).toMatch(/\.homeRunTitle strong\s*\{[^}]*white-space:\s*normal;[^}]*overflow:\s*visible;[^}]*overflow-wrap:\s*anywhere/s)
  })
  it('lets schema fields fill the outer grid without a narrow-screen fixed minimum', () => {
    expect(css).toContain('.schemaInputForm { grid-template-columns: minmax(0, 1fr); }')
    expect(css).toContain('repeat(auto-fit, minmax(min(100%, 240px), 1fr))')
    expect(css).toContain('.schemaInputFields > label:has(textarea) { grid-column: 1 / -1; }')
  })
  it('uses one keyboard-scroll conversation region and wraps unbroken input metadata', () => {
    expect(css).toContain('.conversation .message pre { max-height: none; overflow: visible; }')
    expect(css).toMatch(/\.conversation \.message dl > div, \.conversation \.message dt, \.conversation \.message dd\s*\{[^}]*min-width:\s*0;[^}]*overflow-wrap:\s*anywhere/s)
  })
  it('bounds expanded deletion outputs and uses the existing text token', () => {
    expect(css).toMatch(/\.runDeletionOutputs ul\s*\{[^}]*max-height:\s*240px;[^}]*overflow-y:\s*auto/s)
    expect(css).toContain('var(--text-muted)')
    expect(css).not.toContain('var(--muted)')
  })
})
