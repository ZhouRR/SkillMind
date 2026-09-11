import { marked } from 'marked'

/** 一覧 metadata と実 HTTP stream に適用する同じ preview byte 上限。 */
export const DOCUMENT_PREVIEW_MAX_BYTES = 1_000_000

const HTML_NAMESPACE = 'http://www.w3.org/1999/xhtml'
const SVG_NAMESPACE = 'http://www.w3.org/2000/svg'
const PREVIEW_POLICY = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
const ALLOWED_TAGS = new Set([
  'html', 'head', 'body', 'style', 'details', 'summary', 'nav', 'col', 'colgroup',
  'a', 'abbr', 'article', 'aside', 'b', 'blockquote', 'br', 'caption', 'code', 'dd', 'del',
  'div', 'dl', 'dt', 'em', 'figcaption', 'figure', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'header', 'hr', 'i', 'ins', 'kbd', 'li', 'main', 'mark', 'ol', 'p', 'pre', 'q', 's',
  'samp', 'section', 'small', 'span', 'strong', 'sub', 'sup', 'table', 'tbody', 'td', 'tfoot',
  'th', 'thead', 'tr', 'u', 'ul', 'var', 'wbr',
])
const SVG_TAGS = new Set([
  'svg', 'g', 'defs', 'symbol', 'use', 'path', 'rect', 'circle', 'ellipse', 'line',
  'polyline', 'polygon', 'text', 'tspan', 'textPath', 'title', 'desc', 'style',
  'linearGradient', 'radialGradient', 'stop', 'clipPath', 'mask', 'pattern', 'marker',
  'filter', 'feBlend', 'feColorMatrix', 'feComponentTransfer', 'feComposite',
  'feConvolveMatrix', 'feDiffuseLighting', 'feDisplacementMap', 'feDistantLight',
  'feDropShadow', 'feFlood', 'feFuncA', 'feFuncB', 'feFuncG', 'feFuncR', 'feGaussianBlur',
  'feMerge', 'feMergeNode', 'feMorphology', 'feOffset', 'fePointLight', 'feSpecularLighting',
  'feSpotLight', 'feTile', 'feTurbulence',
])
const RESOURCE_ATTRIBUTES = new Set([
  'src', 'srcset', 'href', 'xlink:href', 'action', 'formaction', 'poster', 'background',
  'data', 'codebase', 'manifest', 'ping', 'srcdoc', 'xml:base', 'autofocus', 'is', 'target',
])
const DROP_CONTENTS = new Set([
  'script', 'template', 'noscript', 'iframe', 'object', 'embed', 'math', 'title',
  'link', 'meta', 'base', 'input', 'button', 'select', 'textarea', 'video', 'audio',
])

/** Markdown の構造を解析した後、HTML と同じ静的 allowlist/CSP に通す。 */
export function documentMarkdownHtml(source: string): string {
  return documentPreviewHtml(envelope(marked.parse(source, { async: false, gfm: true })))
}

/** 埋め込み CSS と静的 SVG は保持し、能動要素を除いた専用 document を sandbox へ渡す。 */
export function documentPreviewHtml(source: string): string {
  if (typeof document === 'undefined') return envelope(`<pre>${escapeText(source)}</pre>`)
  // browsing context のない document で head/body の構造と属性を保持する。原 tree は live DOM に移さない。
  const inert = document.implementation.createHTMLDocument('')
  inert.documentElement.innerHTML = source
  const output = inert.createElement('div')
  const pending: { node: Node; parent: Node }[] = []
  const enqueue = (node: Node, parent: Node): void => {
    for (let child = node.lastChild; child; child = child.previousSibling) pending.push({ node: child, parent })
  }
  pending.push({ node: inert.documentElement, parent: output })
  let visited = 0
  while (pending.length) {
    const item = pending.pop()!
    // 深い/巨大な tree は再帰や部分表示にせず、上限内の原文を inert text として残す。
    if (++visited > 20_000) return envelope(`<pre>${escapeText(source)}</pre>`)
    if (item.node.nodeType === 3) {
      item.parent.appendChild(inert.createTextNode(item.node.textContent ?? ''))
      continue
    }
    if (item.node.nodeType !== 1) continue
    const element = item.node as Element
    const svg = element.namespaceURI === SVG_NAMESPACE
    if (svg ? !SVG_TAGS.has(element.localName)
      : element.namespaceURI !== HTML_NAMESPACE || DROP_CONTENTS.has(element.localName)) continue
    if (element.localName === 'img') {
      item.parent.appendChild(inert.createTextNode(element.getAttribute('alt') ?? ''))
      continue
    }
    if (!svg && !ALLOWED_TAGS.has(element.localName)) { enqueue(element, item.parent); continue }
    const safe = inert.createElementNS(element.namespaceURI, element.localName)
    // CSS は書換えず CSP で外部読取を拒否する。SVG の同一文書参照だけは図形描画に必要。
    for (const attribute of element.attributes) {
      const key = attribute.name.toLowerCase()
      if (key.startsWith('on')) continue
      if (RESOURCE_ATTRIBUTES.has(key) && !(svg && ['href', 'xlink:href'].includes(key)
        && /^#[^\s]+$/.test(attribute.value))) continue
      safe.setAttributeNS(attribute.namespaceURI, attribute.name, attribute.value)
    }
    item.parent.appendChild(safe)
    enqueue(element, safe)
  }
  const root = output.firstElementChild!
  const head = root.querySelector('head')!
  const policy = inert.createElement('meta')
  policy.setAttribute('http-equiv', 'Content-Security-Policy')
  policy.setAttribute('content', PREVIEW_POLICY)
  head.prepend(policy)
  return '<!doctype html>' + root.outerHTML
}

/** 非 DOM/過大 tree の原文 fallback にだけ最小の読み取り用 CSS を添える。 */
function envelope(body: string): string {
  return '<!doctype html><html><head><meta charset="utf-8">'
    + `<meta http-equiv="Content-Security-Policy" content="${PREVIEW_POLICY}">`
    + '<meta name="referrer" content="no-referrer">'
    + '<style>body{font:16px/1.6 system-ui,sans-serif;margin:1rem;overflow-wrap:anywhere}'
    + 'pre{white-space:pre-wrap}table{border-collapse:collapse;display:block;max-width:100%;overflow:auto}'
    + 'td,th{border:1px solid #ccc;padding:.3rem .5rem}blockquote{margin:1rem;border-left:3px solid #ccc;padding-left:1rem}'
    + '</style></head><body>' + body + '</body></html>'
}

/** 非 DOM 環境や複雑な文書の fallback でも source を markup として実行しない。 */
function escapeText(value: string): string {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
}
