import { markdownPreviewPages, type MarkdownPreviewPage } from './markdownPreviewPages'
import { previewTextExceedsLimit } from './previewLimits'

/** Worker は全文を一度だけ受取り、以降は表示対象頁だけを返す。 */
export type MarkdownPreviewWorkerRequest =
  | { type: 'load'; requestId: number; source: string; mode: 'document' | 'reading' }
  | { type: 'page'; requestId: number; index: number }

/** source/token 全頁を message に含めず、失敗も UI で区別できる形にする。 */
export type MarkdownPreviewWorkerResponse =
  | { type: 'page'; requestId: number; page: MarkdownPreviewPage; count: number; index: number }
  | { type: 'error'; requestId: number; failure: 'tooLarge' | 'parseFailed' }

/** browser global に依存しない Worker 内の状態機械。再解析なしに選択頁を取り出す。 */
export function createMarkdownPreviewWorkerModel() {
  let pages: MarkdownPreviewPage[] = []
  return (request: MarkdownPreviewWorkerRequest): MarkdownPreviewWorkerResponse => {
    try {
      if (request.type === 'load') {
        pages = []
        if (previewTextExceedsLimit(request.source)) return { type: 'error', requestId: request.requestId, failure: 'tooLarge' }
        pages = markdownPreviewPages(request.source, request.mode)
      }
      const index = request.type === 'load' ? 0 : request.index
      if (!Number.isInteger(index) || index < 0 || index >= pages.length) {
        return { type: 'error', requestId: request.requestId, failure: 'parseFailed' }
      }
      return { type: 'page', requestId: request.requestId, page: pages[index]!, count: pages.length, index }
    } catch { // lexer の不正入力や stack overflow は UI の原文降級へ渡す。
      pages = []
      return { type: 'error', requestId: request.requestId, failure: 'parseFailed' }
    }
  }
}
