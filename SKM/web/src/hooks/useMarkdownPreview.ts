import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  MARKDOWN_PREVIEW_PAGE_CHARACTERS, markdownPreviewPages, markdownSourcePage, markdownSourcePageCount,
  type MarkdownPreviewPage,
} from '../lib/markdownPreviewPages'
import type { MarkdownPreviewWorkerRequest, MarkdownPreviewWorkerResponse } from '../lib/markdownPreviewWorker'
import { previewTextExceedsLimit } from '../lib/previewLimits'

/** 小さな本文の SSR 互換を保つための解析予算であり、文書の入場上限ではない。 */
export const MARKDOWN_PREVIEW_SYNC_CHARACTERS = MARKDOWN_PREVIEW_PAGE_CHARACTERS
export const MARKDOWN_PREVIEW_WORKER_TIMEOUT_MS = 15_000

/** 入場拒否と処理失敗を分け、UI の案内と原文頁の可否を決める。 */
export type MarkdownPreviewFailure = 'tooLarge' | 'parseFailed' | 'workerUnavailable' | 'timeout'

/** loading 中も選択位置を保持し、旧頁を新しい頁として表示しない。 */
interface PreviewSnapshot {
  status: 'idle' | 'loading' | 'ready' | 'error'
  failure: MarkdownPreviewFailure | null
  page: MarkdownPreviewPage | null
  count: number
  index: number
}

/** 同期で安全に完了する小文書と、独立 Worker が必要な文書を分類する。 */
interface PreparedPreview {
  source: string
  enabled: boolean
  mode: 'document' | 'reading'
  pages: MarkdownPreviewPage[] | null
  initial: PreviewSnapshot
}

/** 解析・頁選択の deadline と所有者を共有し、遅着 message を反映させない。 */
interface PreviewSession {
  owner: PreparedPreview
  worker: Worker | null
  timer: ReturnType<typeof setTimeout> | null
  requestId: number
  deadline: number
  snapshot: PreviewSnapshot
  select: (index: number) => void
}

/** 失敗時にも原文は有界頁で読めるが、入場上限超過は本文を描画しない。 */
function failureSnapshot(source: string, failure: MarkdownPreviewFailure, index = 0): PreviewSnapshot {
  if (failure === 'tooLarge') return { status: 'error', failure, page: null, count: 0, index: 0 }
  const count = markdownSourcePageCount(source)
  const selected = Math.min(Math.max(0, index), count - 1)
  return { status: 'error', failure, page: markdownSourcePage(source, selected), count, index: selected }
}

/** 全文 lex を取消可能な Worker へ移し、main thread には現在頁だけを渡す。 */
export function useMarkdownPreview(source: string, enabled = true, mode: 'document' | 'reading' = 'document') {
  const prepared = useMemo<PreparedPreview>(() => {
    const empty: PreviewSnapshot = { status: enabled ? 'loading' : 'idle', failure: null, page: null, count: 0, index: 0 }
    const owner: PreparedPreview = { source, enabled, mode, pages: null, initial: empty }
    if (!enabled) return owner
    if (previewTextExceedsLimit(source)) return { ...owner, initial: failureSnapshot(source, 'tooLarge') }
    if (source.length <= MARKDOWN_PREVIEW_SYNC_CHARACTERS) {
      try {
        const pages = markdownPreviewPages(source, mode)
        return { ...owner, pages, initial: { status: 'ready', failure: null, page: pages[0]!, count: pages.length, index: 0 } }
      } catch { // 同期予算内の不正構文でも、失敗を隠さず原文頁へ降級する。
        return { ...owner, initial: failureSnapshot(source, 'parseFailed') }
      }
    }
    return owner
  }, [source, enabled, mode])
  const [state, setState] = useState({ owner: prepared, snapshot: prepared.initial })
  const current = useRef(prepared)
  const session = useRef<PreviewSession | null>(null)
  // passive cleanup より先に source が変わっても旧 Worker 応答は適用しない。
  current.current = prepared

  useEffect(() => {
    const active: PreviewSession = { owner: prepared, worker: null, timer: null, requestId: 0, deadline: 0,
      snapshot: prepared.initial, select: () => {} }
    session.current = active
    const owns = (): boolean => current.current === prepared && session.current === active
    const clearTimer = (): void => {
      if (active.timer !== null) { clearTimeout(active.timer); active.timer = null }
    }
    const stopWorker = (): void => {
      clearTimer()
      active.worker?.terminate()
      active.worker = null
    }
    const publish = (snapshot: PreviewSnapshot): void => {
      if (!owns()) return
      active.snapshot = snapshot
      setState({ owner: prepared, snapshot })
    }
    const fail = (failure: MarkdownPreviewFailure): void => {
      if (!owns()) return
      stopWorker()
      publish(failureSnapshot(source, failure))
    }
    const send = (request: MarkdownPreviewWorkerRequest): void => {
      clearTimer()
      active.deadline = performance.now() + MARKDOWN_PREVIEW_WORKER_TIMEOUT_MS
      active.timer = setTimeout(() => fail('timeout'), MARKDOWN_PREVIEW_WORKER_TIMEOUT_MS)
      try { active.worker!.postMessage(request) }
      catch { fail('parseFailed') } // 構造 clone/起動失敗も無制限の同期再解析へ戻さない。
    }
    active.select = (index): void => {
      if (!owns() || !Number.isInteger(index) || index < 0 || index >= active.snapshot.count) return
      if (prepared.pages) {
        publish({ status: 'ready', failure: null, page: prepared.pages[index]!, count: prepared.pages.length, index })
      } else if (active.snapshot.failure) {
        publish(failureSnapshot(source, active.snapshot.failure, index))
      } else if (active.worker && index !== active.snapshot.index) {
        publish({ ...active.snapshot, status: 'loading', page: null, index })
        send({ type: 'page', requestId: ++active.requestId, index })
      }
    }
    if (prepared.enabled && prepared.initial.status === 'loading') {
      if (typeof Worker === 'undefined') fail('workerUnavailable')
      else {
        try {
          const worker = new Worker(new URL('../workers/markdownPreview.worker.ts', import.meta.url), { type: 'module' })
          active.worker = worker
          worker.onmessage = (event: MessageEvent<MarkdownPreviewWorkerResponse>): void => {
            if (!owns() || active.worker !== worker || active.timer === null || event.data.requestId !== active.requestId) return
            if (performance.now() >= active.deadline) { fail('timeout'); return }
            clearTimer()
            if (event.data.type === 'error') { fail(event.data.failure); return }
            publish({ status: 'ready', failure: null, page: event.data.page, count: event.data.count, index: event.data.index })
          }
          worker.onerror = (): void => { if (active.worker === worker) fail('parseFailed') }
          worker.onmessageerror = (): void => { if (active.worker === worker) fail('parseFailed') }
          send({ type: 'load', requestId: ++active.requestId, source, mode })
        } catch { fail('workerUnavailable') } // CSP/非対応 browser の起動例外も明示する。
      }
    }
    return () => {
      stopWorker()
      if (session.current === active) session.current = null
    }
  }, [prepared, source, mode])

  const select = useCallback((index: number): void => {
    if (session.current?.owner === prepared && current.current === prepared) session.current.select(index)
  }, [prepared])
  return { ...(state.owner === prepared ? state.snapshot : prepared.initial), select }
}
