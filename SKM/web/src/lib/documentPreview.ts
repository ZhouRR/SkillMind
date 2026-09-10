/** 一覧 metadata と実 HTTP stream に適用する同じ preview byte 上限。 */
export const DOCUMENT_PREVIEW_MAX_BYTES = 1_000_000

const HTML_NAMESPACE = 'http://www.w3.org/1999/xhtml'
const PREVIEW_POLICY = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
const ALLOWED_TAGS = new Set([
  'a', 'abbr', 'article', 'aside', 'b', 'blockquote', 'br', 'caption', 'code', 'dd', 'del',
  'div', 'dl', 'dt', 'em', 'figcaption', 'figure', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'header', 'hr', 'i', 'ins', 'kbd', 'li', 'main', 'mark', 'ol', 'p', 'pre', 'q', 's',
  'samp', 'section', 'small', 'span', 'strong', 'sub', 'sup', 'table', 'tbody', 'td', 'tfoot',
  'th', 'thead', 'tr', 'u', 'ul', 'var', 'wbr',
])
const DROP_CONTENTS = new Set([
  'script', 'style', 'template', 'noscript', 'iframe', 'object', 'embed', 'svg', 'math',
  'link', 'meta', 'base', 'input', 'button', 'select', 'textarea', 'video', 'audio',
])

/** Sandbox に加え、URL/能動要素/CSS を持たない静的 HTML だけを新しい tree に再構成する。 */
export function documentPreviewHtml(source: string): string {
  if (typeof document === 'undefined') return envelope(`<pre>${escapeText(source)}</pre>`)
  // browsing context のない document の template 内容は live DOM に移さない。
  const inert = document.implementation.createHTMLDocument('')
  const template = inert.createElement('template')
  template.innerHTML = source
  const output = inert.createElement('div')
  const pending: { node: Node; parent: Node }[] = []
  const enqueue = (node: Node, parent: Node): void => {
    for (let child = node.lastChild; child; child = child.previousSibling) pending.push({ node: child, parent })
  }
  enqueue(template.content, output)
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
    if (element.namespaceURI !== HTML_NAMESPACE || DROP_CONTENTS.has(element.localName)) continue
    if (element.localName === 'img') {
      item.parent.appendChild(inert.createTextNode(element.getAttribute('alt') ?? ''))
      continue
    }
    if (!ALLOWED_TAGS.has(element.localName)) { enqueue(element, item.parent); continue }
    const safe = inert.createElement(element.localName)
    // style、URL、id/name、event handler は一切コピーしない。
    for (const key of ['title', 'lang', 'dir', 'colspan', 'rowspan']) {
      const value = element.getAttribute(key)
      if (value !== null && (key !== 'dir' || ['ltr', 'rtl', 'auto'].includes(value))
        && (!['colspan', 'rowspan'].includes(key) || /^[1-9]\d{0,2}$/.test(value))) safe.setAttribute(key, value)
    }
    item.parent.appendChild(safe)
    enqueue(element, safe)
  }
  return envelope(output.innerHTML)
}

/** CSP は全 user content より前、表示 CSS は平台の固定文字列だけに限定する。 */
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
