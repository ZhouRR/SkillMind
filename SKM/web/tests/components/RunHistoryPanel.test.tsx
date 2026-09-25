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

  it('omits unusable page buttons for a single page and names sources for people', () => {
    const html = renderToStaticMarkup(<LanguageProvider language="ja"><RunHistoryPanel
      state={{ status: 'ready', page: { items: [{
        run_id: '00000000-0000-4000-8000-000000000501', project_id: '00000000-0000-4000-8000-000000000001',
        task_id: 'task', task_title: 'Spec review', status: 'SUCCEEDED', row_version: 1, input: {},
        created_at: '2026-09-25T03:14:05Z', started_at: '2026-09-25T03:14:05Z', finished_at: '2026-09-25T03:27:21Z',
        result_summary: 'Done', result_confidence: null, result_needs_review: null,
        selected_sources: { documents: 'project-documents', database: 'postgres', library: 'project-library', extra: 'future-provider' },
      }], offset: 0, limit: 20, has_more: false } }}
      selectedRunId={null} onOpen={() => {}} onNext={() => {}} onPrevious={() => {}} onRefresh={() => {}} />
    </LanguageProvider>)
    // 一頁で完結する一覧は前後 button を出さず、件数範囲だけを示す。
    expect(html).not.toContain(MESSAGES.ja.runHistory.previous)
    expect(html).not.toContain(MESSAGES.ja.runHistory.next)
    expect(html).toContain('1–1')
    // 取得元 ID は表示名へ変換し、未知の ID は原文のまま残す。
    expect(html).toContain('プロジェクト文書 / PostgreSQL / プロジェクト文書庫 / future-provider')
    expect(html).not.toContain('project-documents')
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
