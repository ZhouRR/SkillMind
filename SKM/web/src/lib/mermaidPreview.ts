import { marked, type Token, type Tokens } from 'marked'
import { documentPreviewHtml } from './documentPreview'

export const MERMAID_MAX_CHARACTERS = 8_000
export const MERMAID_MAX_NODES = 80
export const MERMAID_MAX_EDGES = 120
export const MERMAID_MAX_PAGE_DIAGRAMS = 8

/** 図の生成状態は code token ごとに持ち、同一原文の図も SVG id を共有しない。 */
export type MermaidPreview = { status: 'ready'; document: string; svg: string; height: number }
  | { status: 'loading' | 'failed' }
export type MermaidPreviews = ReadonlyMap<Tokens.Code, MermaidPreview>
/** UI 文言は呼出側の三語 catalog から受取る。 */
export interface MermaidPreviewLabels { loading: string; failed: string; title: string }

/** HTML の class や通常の矢印文を図と誤認せず、明示 fenced code のみを選ぶ。 */
export function isMermaidCode(token: Tokens.Code): boolean {
  return token.lang?.trim().toLowerCase() === 'mermaid'
    && /^ {0,3}(?:`{3,}|~{3,})[ \t]*mermaid[ \t]*(?:\r?\n|$)/i.test(token.raw)
}

/** 現在頁の入れ子リスト・引用も marked の token traversal と同じ順で処理する。 */
export function mermaidCodeTokens(tokens: Token[]): Tokens.Code[] {
  const result: Tokens.Code[] = []
  marked.walkTokens(tokens, (token) => {
    if (token.type === 'code' && typeof token.text === 'string' && isMermaidCode(token as Tokens.Code)) {
      result.push(token as Tokens.Code)
    }
  })
  return result
}

/** 外部資源を生成し得る拡張構文を解釈しない、小さい flowchart 専用 parser。 */
export function safeMermaidFlowchart(source: string): string | null {
  if (source.length > MERMAID_MAX_CHARACTERS) return null
  const header = /^\s*(?:flowchart|graph)[ \t]+(TD|TB|BT|LR|RL)[ \t]*(?:;|\r?\n|$)/.exec(source)
  if (!header) return null
  let offset = header[0].length
  let edges = 0
  const nodes = new Map<string, string>()
  const statements: string[] = []
  const whitespace = (): void => { while (/[ \t]/.test(source[offset] ?? '\0')) offset++ }
  const node = (): string | null => {
    whitespace()
    const match = /^[A-Za-z_][A-Za-z0-9_]{0,63}/.exec(source.slice(offset))
    if (!match) return null
    offset += match[0].length
    if (!nodes.has(match[0])) {
      if (nodes.size >= MERMAID_MAX_NODES) return null
      nodes.set(match[0], `n${nodes.size}`)
    }
    const id = nodes.get(match[0])!
    whitespace()
    const shapes = [['((', '))'], ['([', '])'], ['[', ']'], ['(', ')'], ['{', '}']] as const
    const shape = shapes.find(([open]) => source.startsWith(open, offset))
    if (!shape) return id
    offset += shape[0].length
    const end = source.indexOf(shape[1], offset)
    if (end < 0) return null
    const label = safeLabel(source.slice(offset, end))
    if (label === null) return null
    offset = end + shape[1].length
    return `${id}${shape[0]}"${label}"${shape[1]}`
  }
  while (offset < source.length) {
    while (/[\s;]/.test(source[offset] ?? '\0')) offset++
    if (offset >= source.length) break
    let statement = node()
    if (statement === null) return null
    while (offset < source.length) {
      whitespace()
      if (/[\r\n;]/.test(source[offset] ?? '') || offset >= source.length) break
      const arrow = /^(-->|---|-\.->|==>)/.exec(source.slice(offset))
      if (!arrow || ++edges > MERMAID_MAX_EDGES) return null
      offset += arrow[0].length
      whitespace()
      let label = ''
      if (source[offset] === '|') {
        const end = source.indexOf('|', offset + 1)
        if (end < 0) return null
        const text = safeLabel(source.slice(offset + 1, end))
        if (text === null) return null
        label = `|"${text}"|`
        offset = end + 1
      }
      const target = node()
      if (target === null) return null
      statement += ` ${arrow[0]}${label} ${target}`
    }
    statements.push(statement)
  }
  if (!nodes.size) return null
  // 元 id は予約語や class 構文になり得るため、Mermaid へは自前の id と純粋な label のみ渡す。
  const defaults = [...nodes].map(([label, id]) => `${id}["${label}"]`)
  return `flowchart ${header[1]}\n${defaults.join('\n')}\n${statements.join('\n')}`
}

/** HTML/entity/icon/math/Markdown の解釈経路を閉じ、Unicode の通常 label を保持する。 */
function safeLabel(input: string): string | null {
  const value = input.trim().replace(/^"(.*)"$/, '$1')
  return value.length > 200 || /[<>"`\\&@#$%:|\r\n\u0000-\u001f\u007f]/.test(value) ? null : value
}

/** 初回の有効な図だけが大きい依存 chunk を取得する。設定は原文で上書きできない。 */
let mermaidModule: Promise<typeof import('mermaid')> | null = null
let nextDiagramId = 0
let renderQueue: Promise<unknown> = Promise.resolve()

/** Mermaid の共有設定と DOM を直列化し、取消済みの待ち行列は描画せずに捨てる。 */
export function renderMermaidPreview(source: string, signal: AbortSignal): Promise<MermaidPreview> {
  const safe = safeMermaidFlowchart(source)
  if (!safe || signal.aborted || typeof document === 'undefined') return Promise.resolve({ status: 'failed' })
  const result = renderQueue.then(async (): Promise<MermaidPreview> => {
    if (signal.aborted) return { status: 'failed' }
    mermaidModule ??= import('mermaid').catch((error: unknown) => { mermaidModule = null; throw error })
    const { default: mermaid } = await mermaidModule
    if (signal.aborted) return { status: 'failed' }
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', htmlLabels: false,
      maxTextSize: MERMAID_MAX_CHARACTERS * 3, maxEdges: MERMAID_MAX_EDGES,
      suppressErrorRendering: true, theme: 'neutral',
      flowchart: { htmlLabels: false, useMaxWidth: false },
      secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'maxEdges', 'htmlLabels', 'flowchart', 'theme'],
    })
    const host = document.createElement('div')
    host.setAttribute('aria-hidden', 'true')
    host.style.cssText = 'position:fixed;left:-100000px;top:0;visibility:hidden;pointer-events:none'
    document.body.append(host)
    const removeHost = (): void => host.remove()
    signal.addEventListener('abort', removeHost, { once: true })
    try {
      // 事前 parser が生成した DSL だけを live DOM を使う Mermaid へ渡す。bindFunctions は呼ばない。
      const { svg } = await mermaid.render(`skillmindMermaid${++nextDiagramId}`, safe, host)
      if (signal.aborted) return { status: 'failed' }
      const doc = documentPreviewHtml('<!doctype html><html><head><style>'
        + ':root{color-scheme:light}body{margin:8px;background:#fff;color:#222;overflow:auto}'
        + 'svg{display:block;max-width:none;background:#fff}'
        + '</style></head><body>' + svg + '</body></html>')
      const inert = document.implementation.createHTMLDocument('')
      inert.documentElement.innerHTML = doc
      const diagram = inert.querySelector('svg')
      if (!diagram) return { status: 'failed' }
      const viewBox = diagram.getAttribute('viewBox')?.split(/[ ,]+/).map(Number)
      const height = viewBox?.length === 4 && Number.isFinite(viewBox[3]) ? Math.ceil(viewBox[3]! + 24) : 240
      return { status: 'ready', document: doc, svg: diagram.outerHTML, height: Math.max(120, Math.min(520, height)) }
    } finally { signal.removeEventListener('abort', removeHost); host.remove() }
  }).catch((): MermaidPreview => ({ status: 'failed' })) // import/構文/layout 失敗は安全な原文 UI へ戻す。
  renderQueue = result
  return result
}

/** 図の CSS を reading 本文へ漏らさず、無権限の静的 iframe として埋め込む。 */
export function mermaidCodeHtml(token: Tokens.Code, previews: MermaidPreviews,
  mode: 'document' | 'reading', labels: MermaidPreviewLabels): string | false {
  if (!isMermaidCode(token)) return false
  const preview = previews.get(token)
  if (preview?.status === 'ready') {
    if (mode === 'document') return `<figure class="mermaidPreview" aria-label="${escapeMarkup(labels.title)}"`
      + ' style="margin:1rem 0;max-width:100%;overflow:auto;background:#fff;color:#222;padding:8px;border-radius:6px">'
      + preview.svg + '</figure>'
    return `<iframe class="mermaidPreview" sandbox="" referrerpolicy="no-referrer" title="${escapeMarkup(labels.title)}"`
      + ` style="display:block;width:100%;height:${preview.height}px;border:0;background:#fff" srcdoc="${escapeMarkup(preview.document)}"></iframe>`
  }
  return `<p class="hint" role="status">${escapeMarkup(preview?.status === 'failed' ? labels.failed : labels.loading)}</p>`
    + `<pre><code class="language-mermaid">${escapeMarkup(token.text)}</code></pre>`
}

/** 属性値にも原文にも共通で使用し、fallback を markup として解釈させない。 */
function escapeMarkup(value: string): string {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;')
}
