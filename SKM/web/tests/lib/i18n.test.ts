import { describe, expect, it } from 'vitest'

import { MESSAGES, UI_LANGUAGES } from '../../src/lib/i18n/messages'
import { asUiLanguage, resolveUiLanguage } from '../../src/lib/i18n/resolve'
import { APP_ROUTES } from '../../src/lib/routing'

describe('resolveUiLanguage', () => {
  it('prefers a valid saved preference over browser languages', () => {
    expect(resolveUiLanguage('ja', ['en-US'])).toBe('ja')
  })

  it('falls back to the first matching browser language when unset', () => {
    expect(resolveUiLanguage(null, ['fr-FR', 'en-GB', 'ja'])).toBe('en')
    expect(resolveUiLanguage(undefined, ['zh-CN'])).toBe('zh')
  })

  it('defaults to zh when nothing matches', () => {
    expect(resolveUiLanguage(null, ['fr-FR', 'de-DE'])).toBe('zh')
    expect(resolveUiLanguage('fr', [])).toBe('zh')
  })

  it('narrows unknown values to null via asUiLanguage', () => {
    expect(asUiLanguage('en')).toBe('en')
    expect(asUiLanguage('EN')).toBeNull()
    expect(asUiLanguage(null)).toBeNull()
  })
})

describe('message catalogs', () => {
  it('covers every route in every language', () => {
    // Route 追加時に catalog の翻訳漏れを compile と両方で検出する。
    for (const language of UI_LANGUAGES) {
      for (const { route } of APP_ROUTES) {
        expect(MESSAGES[language].routes[route].label).not.toBe('')
        expect(MESSAGES[language].routes[route].description).not.toBe('')
      }
    }
  })

  it('keeps the zh catalog identical to the historical hardcoded labels', () => {
    // 既定言語 zh の表示は資源化前と一字一句変わらないことを固定する。
    expect(MESSAGES.zh.routes.home.label).toBe('概览')
    expect(MESSAGES.zh.routes.workspace.label).toBe('工作空间')
    expect(MESSAGES.zh.nav.currentProject).toBe('当前项目')
    expect(MESSAGES.zh.nav.logout).toBe('退出登录')
    expect(MESSAGES.zh.login.title).toBe('登录')
  })

  it('provides distinct translations for ja and en shell labels', () => {
    expect(MESSAGES.ja.nav.logout).toBe('ログアウト')
    expect(MESSAGES.en.nav.logout).toBe('Sign out')
    expect(MESSAGES.ja.routes.workspace.label).not.toBe(MESSAGES.zh.routes.workspace.label)
    expect(MESSAGES.en.routes.workspace.label).not.toBe(MESSAGES.zh.routes.workspace.label)
  })

  it('keeps Japanese navigation and skill scope labels concise', () => {
    expect(MESSAGES.ja.nav.currentProject).toBe('プロジェクト')
    expect(MESSAGES.ja.skills.scopeBadgeWithProject).toBe('組織資産 · プロジェクトで設定可能')
  })
})
