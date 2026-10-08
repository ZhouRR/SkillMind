// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import shortStyles from '../../src/styles/short-controls.css?raw'
import baseStyles from '../../src/styles/base.css?raw'
import pageStyles from '../../src/styles/pages.css?raw'
import accountStyles from '../../src/styles/accounts.css?raw'
import workspaceStyles from '../../src/styles/workspace.css?raw'
import responsiveStyles from '../../src/styles/workspace-responsive.css?raw'
import schemaStyles from '../../src/styles/workspace-audit.css?raw'
import resourceStyles from '../../src/styles/resources-audit.css?raw'
import evaluationStyles from '../../src/styles/evaluations.css?raw'

let stylesheet: HTMLStyleElement
let fixture: HTMLDivElement
/** 実入口と同じ基盤/画面/短い field の順序で cascade を検証する。 */
beforeEach(() => {
  stylesheet = document.createElement('style')
  stylesheet.textContent = [baseStyles, pageStyles, accountStyles, workspaceStyles,
    responsiveStyles, schemaStyles, resourceStyles, shortStyles, evaluationStyles].join('\n')
  document.head.append(stylesheet)
  fixture = document.createElement('div')
  document.body.append(fixture)
})
afterEach(() => { stylesheet.remove(); fixture.remove() })

/** 実 stylesheet の cascade を確認し、jsdom を画素 layout の代用にはしない。 */
function style(selector: string): CSSStyleDeclaration { return getComputedStyle(fixture.querySelector(selector)!) }

describe('scoped short control styles', () => {
  it.each(['accountForm', 'scheduleConfiguration', 'schemaInputFields'])('caps short selects inside %s while retaining 42px form targets and long text wrapping', (scope) => {
    fixture.innerHTML = `<div class="${scope}"><label><button class="selectTrigger shortControl"><span class="selectValue">Long translated label</span></button></label><label><button class="selectTrigger">Long resource</button></label></div>`
    expect(style('.shortControl').width).toBe('min(100%, 168px)')
    expect(style('.shortControl').minHeight).toBe('42px')
    expect(style('.selectValue').overflowWrap).toBe('anywhere')
    expect(style('.selectTrigger:not(.shortControl)').width).toBe('100%')
  })
  it.each(['skillLibraryFilters', 'taskFilters'])('reserves only 12rem for short filters in %s', (scope) => {
    fixture.innerHTML = `<div class="${scope}"><label><input /></label><label class="shortField"><button class="selectTrigger shortControl"></button></label></div>`
    expect(style('.shortField').flex).toBe('0 1 12rem')
    expect(style('.shortField').width).toBe('min(100%, 168px)')
    expect(style('input').width).toBe('100%')
  })
  it('caps sort, score and known numeric fields independently of default controls', () => {
    fixture.innerHTML = '<div class="documentManagementToolbar"><label class="documentSort shortField"><button class="selectTrigger shortControl shortControlNarrow"></button></label></div><div class="evaluationSection"><div class="formRow evaluationRatingFields"><label><button class="selectTrigger shortControl shortControlNarrow"></button></label><label><button class="selectTrigger"></button></label></div></div><div class="resourceFormFields"><label><input class="shortNumberControl" type="number" /></label><label><input type="number" /></label></div>'
    expect(style('.documentSort .selectTrigger').width).toBe('min(100%, 126px)')
    expect(style('.evaluationRatingFields').gridTemplateColumns).toBe('minmax(0, 9rem) minmax(0, 1fr)')
    expect(style('.evaluationSection .shortControl').width).toBe('min(100%, 126px)')
    expect(style('input.shortNumberControl').width).toBe('min(100%, 140px)')
    expect(style('input.shortNumberControl').minHeight).toBe('42px')
    expect(style('input:not(.shortNumberControl)').width).toBe('100%')
  })
  it('gives the time input remaining space beside a compact weekday', () => {
    fixture.innerHTML = '<div class="cronPresetFields"><label><input type="time" /></label><label class="shortField"><button class="selectTrigger shortControl"></button></label></div>'
    expect(style('.cronPresetFields').gridTemplateColumns).toBe('minmax(0, 1fr) minmax(0, 12rem)')
    expect(style('input').width).toBe('100%')
  })
  it('keeps the shared compact popup, checkmark and coarse-pointer size contracts', () => {
    fixture.innerHTML = '<div class="selectPopup" data-density="compact"><div class="selectItem"><span class="selectItemIndicator"></span></div></div>'
    expect(style('.selectPopup').width).toBe('max(var(--anchor-width), 9rem)')
    expect(style('.selectPopup').scrollbarGutter).toBe('auto')
    expect(style('.selectItem').minHeight).toBe('36px')
    expect(style('.selectItemIndicator').flexBasis).toBe('16px')
    expect(baseStyles).toMatch(/@media \(pointer: coarse\)\s*\{\s*\.selectPopup\[data-density='compact'\] \.selectItem \{ min-height: 44px; \}/)
    expect(shortStyles).toContain('@media (max-width: 600px)')
    expect(shortStyles).toContain("button.selectTrigger.shortControl, input[type='number'].shortNumberControl { width: 100%; }")
  })
})
