// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import baseStyles from '../../src/styles/base.css?raw'
import pageStyles from '../../src/styles/pages.css?raw'
import accountStyles from '../../src/styles/accounts.css?raw'
import shellStyles from '../../src/styles/shell.css?raw'
import menuStyles from '../../src/styles/action-menu.css?raw'

let stylesheet: HTMLStyleElement
let fixture: HTMLDivElement

/** 実 CSS の cascade を確認する。pixel の対称性は browser runner で別途検証する。 */
beforeEach(() => {
  stylesheet = document.createElement('style')
  stylesheet.textContent = [baseStyles, pageStyles, accountStyles, menuStyles, shellStyles].join('\n')
  document.head.append(stylesheet)
  fixture = document.createElement('div')
  document.body.append(fixture)
})
afterEach(() => { stylesheet.remove(); fixture.remove() })

/** 対象 surface の computed declaration を取得し、page 固有 override も通す。 */
function styleFor(className: string): CSSStyleDeclaration {
  fixture.className = className
  return getComputedStyle(fixture)
}

describe('popup spacing style contracts', () => {
  it('keeps explicitly compact short-option popups narrow without reserving empty scrollbars', () => {
    fixture.className = 'selectPopup'
    fixture.dataset.density = 'compact'
    fixture.innerHTML = '<div class="selectItem"><span class="selectItemText">English</span><span class="selectItemIndicator"></span></div>'
    expect(getComputedStyle(fixture).width).toBe('max(var(--anchor-width), 9rem)')
    expect(getComputedStyle(fixture).scrollbarGutter).toBe('auto')
    expect(getComputedStyle(fixture).padding).toBe('4px')
    const item = fixture.firstElementChild!
    expect(getComputedStyle(item).minHeight).toBe('36px')
    expect(getComputedStyle(item).padding).toBe('6px 10px')
    expect(getComputedStyle(item).gap).toBe('8px')
    expect(getComputedStyle(item.lastElementChild!).flexBasis).toBe('16px')
    fixture.dataset.overflow = 'true'
    expect(getComputedStyle(fixture).scrollbarGutter).toBe('stable both-edges')
    fixture.removeAttribute('data-overflow')
    fixture.removeAttribute('data-density')
    expect(getComputedStyle(fixture).width).toBe('max(var(--anchor-width), 12rem)')
    expect(getComputedStyle(fixture).scrollbarGutter).toBe('stable both-edges')
    expect(getComputedStyle(item).minHeight).toBe('44px')
  })
  it('does not double-count the native arrow padding on the language trigger', () => {
    fixture.className = 'sidebar'
    fixture.innerHTML = '<label class="sidebarLanguage"><button class="selectTrigger" data-density="compact">English</button></label>'
    const trigger = fixture.querySelector('button')!
    expect(getComputedStyle(trigger).paddingLeft).toBe('10px')
    expect(getComputedStyle(trigger).paddingRight).toBe('10px')
  })

  it.each(['selectPopup', 'selectPopup documentFolderPopup', 'accountEventList'])(
    'balances reserved scrollbar space on %s', (className) => {
      expect(styleFor(className).scrollbarGutter).toBe('stable both-edges')
    },
  )
  it.each(['selectPopup', 'selectPopup documentFolderPopup', 'actionMenuPanel', 'selectItem'])(
    'keeps physical item and surface padding equal on %s', (className) => {
      const style = styleFor(className)
      expect(style.paddingLeft).toBe(style.paddingRight)
    },
  )
  it('reserves menu gutters only when the actual menu overflows', () => {
    fixture.className = 'actionMenuPanel'
    expect(getComputedStyle(fixture).scrollbarGutter).toBe('auto')
    expect(getComputedStyle(fixture).padding).toBe('4px')
    fixture.dataset.overflow = 'true'
    expect(getComputedStyle(fixture).scrollbarGutter).toBe('stable both-edges')
    fixture.innerHTML = '<button class="actionMenuItem">Remove</button>'
    expect(getComputedStyle(fixture.firstElementChild!).minHeight).toBe('36px')
  })
  it('uses equal logical spacing on the bounded account event list', () => {
    expect(styleFor('accountEventList').paddingInline).toBe('4px')
    expect(styleFor('accountEventList').paddingInlineEnd).toBe(styleFor('accountEventList').paddingInlineStart)
  })
  it('does not reserve empty scrollbar space on an unbounded user list', () => {
    const style = styleFor('accountUserList')
    expect(style.scrollbarGutter).not.toContain('stable')
    expect(style.overflow).not.toBe('auto')
    expect(Number.parseFloat(style.paddingLeft)).toBe(0)
    expect(Number.parseFloat(style.paddingRight)).toBe(0)
  })
  it('keeps short dialog content aligned with its header', () => {
    const header = styleFor('modalHeader').paddingLeft
    const body = styleFor('modalBody')
    expect(body.scrollbarGutter).toBe('auto')
    expect(body.paddingLeft).toBe(header)
    expect(body.paddingRight).toBe(header)
  })
  it('retains the checkmark slot without displaying unselected indicators', () => {
    fixture.className = 'selectItemIndicator'
    expect(getComputedStyle(fixture).visibility).toBe('hidden')
    expect(getComputedStyle(fixture).flexBasis).toBe('16px')
    fixture.dataset.selected = ''
    expect(getComputedStyle(fixture).visibility).toBe('visible')
    expect(getComputedStyle(fixture).flexBasis).toBe('16px')
  })
})
