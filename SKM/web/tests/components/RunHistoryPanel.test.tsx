import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { RunHistoryPanel } from '../../src/components/RunHistoryPanel'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, type UiLanguage } from '../../src/lib/i18n/messages'

/** API の空ページを描画し、初回空状態と一覧変更後のページ切れを区別する。 */
function renderEmpty(language: UiLanguage, offset = 0, trashed = false): string {
  return renderToStaticMarkup(<LanguageProvider language={language}><RunHistoryPanel
    state={{ status: 'ready', page: { items: [], offset, limit: 20, has_more: false } }}
    selectedRunId={null} trashed={trashed} onOpen={() => {}} onNext={() => {}}
    onPrevious={() => {}} onRefresh={() => {}} />
  </LanguageProvider>)
}

describe('run history empty pages', () => {
  it.each(['zh', 'ja', 'en'] as const)('distinguishes an empty recycle bin from no project runs in %s', (language) => {
    expect(renderEmpty(language, 0, true)).toContain(MESSAGES[language].runHistory.emptyTrash)
    expect(renderEmpty(language, 0, true)).not.toContain(MESSAGES[language].runHistory.empty)
    expect(renderEmpty(language)).toContain(MESSAGES[language].runHistory.empty)
  })

  it.each([false, true])('keeps an enabled previous-page action after rows disappear, trash=%s', (trashed) => {
    const html = renderEmpty('en', 20, trashed)
    expect(html).toContain(MESSAGES.en.runHistory.emptyPage)
    expect(html).toMatch(/<button[^>]*type="button">Previous<\/button>/)
    expect(html).not.toMatch(/<button[^>]*disabled[^>]*>Previous<\/button>/)
    expect(html).toMatch(/<button[^>]*disabled[^>]*>Next<\/button>/)
    expect(html).not.toContain('21–20')
  })
})
