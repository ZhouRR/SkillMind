import { useEffect, useMemo, useRef, useState } from 'react'
import type { Token, Tokens } from 'marked'
import { MERMAID_MAX_PAGE_DIAGRAMS, mermaidCodeTokens, renderMermaidPreview,
  safeMermaidFlowchart, type MermaidPreview, type MermaidPreviews } from '../lib/mermaidPreview'

export const MERMAID_PREVIEW_TIMEOUT_MS = 15_000

/** 図は表示中の頁だけ遅延生成し、頁切替・非表示・unmount 後の応答を破棄する。 */
export function useMermaidPreviews(tokens?: Token[], enabled = true): MermaidPreviews {
  const owner = useMemo(() => {
    const codes = enabled && tokens ? mermaidCodeTokens(tokens) : []
    const initial = new Map<Tokens.Code, MermaidPreview>()
    codes.forEach((token, index) => initial.set(token, { status:
      index < MERMAID_MAX_PAGE_DIAGRAMS && safeMermaidFlowchart(token.text) ? 'loading' : 'failed' }))
    return { codes, initial }
  }, [tokens, enabled])
  const current = useRef(owner)
  current.current = owner
  const [state, setState] = useState({ owner, previews: owner.initial })
  useEffect(() => {
    if (!owner.codes.length) return
    const abort = new AbortController()
    const deadline = performance.now() + MERMAID_PREVIEW_TIMEOUT_MS
    const previews = new Map(owner.initial)
    const owns = (): boolean => current.current === owner && !abort.signal.aborted
    const publish = (): void => { if (owns()) setState({ owner, previews: new Map(previews) }) }
    const fail = (): void => {
      if (!owns()) return
      for (const [token, result] of previews) if (result.status === 'loading') previews.set(token, { status: 'failed' })
      publish()
      abort.abort()
    }
    const timer = setTimeout(fail, MERMAID_PREVIEW_TIMEOUT_MS)
    // 一頁の上限に加えて逐次処理し、cancel 後は次の import/render を開始しない。
    void (async () => {
      for (const token of owner.codes) {
        if (!owns()) break
        if (previews.get(token)?.status !== 'loading') continue
        const preview = await renderMermaidPreview(token.text, abort.signal)
        if (!owns()) break
        // 同期 layout は timer で割込めないため、timer 配送前の遅延成功も受付けない。
        if (performance.now() >= deadline) { fail(); break }
        previews.set(token, preview)
        publish()
      }
      clearTimeout(timer)
    })()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [owner])
  return state.owner === owner ? state.previews : owner.initial
}
