import { marked, type Token, type Tokens } from 'marked'

/** 文字数と token tree の複雑さを別々に抑え、一頁だけを描画する。 */
export const MARKDOWN_PREVIEW_PAGE_CHARACTERS = 32_000
// 短い参照 link が大きい解決済み payload を共有しても、頁 metadata と selector DOM を増幅させない。
export const MARKDOWN_PREVIEW_MAX_PAGES = 2_048
const PAGE_WEIGHT = 32_000
const TABLE_ROWS = 80
const TOKEN_WEIGHT = 24
// 改行・構文記号は block/inline token 分岐の指標。大量の短行・cell による lex 時の増幅を先に止める。
const LEX_STRUCTURE_BUDGET = 500_000
const STRUCTURE_MARKERS = new Set('\n\r*_~`[]!>|-#+.():<\\')
// 境界を surrogate pair の前へずらしても一頁の文字数予算を超えない。
const SOURCE_SLICE = MARKDOWN_PREVIEW_PAGE_CHARACTERS - 1

/** 巨大な不可分 block は明示的な原文頁にし、巨大な DOM を作らない。 */
export interface MarkdownPreviewPage {
  tokens: Token[]
  source: string
  heading: string
  sourceOnly: boolean
}

/** 入場済み本文を拒否せず、token 配列が膨張する密な構文だけを有界原文頁へ回す。 */
function exceedsLexStructureBudget(source: string): boolean {
  let structure = 0
  for (let index = 0; index < source.length; index++) {
    if (STRUCTURE_MARKERS.has(source[index]!) && ++structure > LEX_STRUCTURE_BUDGET) return true
  }
  return false
}

/** renderer の再帰と Worker message の clone 量も原文量と一緒に制限する。 */
function tokenWeight(value: unknown): number {
  const pending: Array<{ value: unknown; depth: number }> = [{ value, depth: 0 }]
  let weight = 0
  while (pending.length && weight <= PAGE_WEIGHT) {
    const item = pending.pop()!
    if (item.depth > 80) return PAGE_WEIGHT + 1
    if (typeof item.value === 'string') weight += item.value.length
    else if (Array.isArray(item.value)) {
      weight += item.value.length
      if (weight > PAGE_WEIGHT) return weight
      for (const child of item.value) pending.push({ value: child, depth: item.depth + 1 })
    } else if (item.value && typeof item.value === 'object') {
      weight += TOKEN_WEIGHT
      for (const child of Object.values(item.value)) pending.push({ value: child, depth: item.depth + 1 })
    }
  }
  return weight
}

/** 原文降級は必要な一頁だけを切り出し、全文の配列を main thread に作らない。 */
export function markdownSourcePageCount(source: string): number {
  return Math.max(1, Math.ceil(source.length / SOURCE_SLICE))
}

/** UTF-16 の文字対を保ち、巨大 block と解析失敗の原文を同じ予算で表示する。 */
export function markdownSourcePage(source: string, index: number, heading = ''): MarkdownPreviewPage {
  const boundary = (offset: number): number => {
    const position = Math.min(source.length, offset)
    const previous = source.charCodeAt(position - 1)
    const next = source.charCodeAt(position)
    return previous >= 0xd800 && previous <= 0xdbff && next >= 0xdc00 && next <= 0xdfff ? position - 1 : position
  }
  return { tokens: [], source: source.slice(boundary(index * SOURCE_SLICE), boundary((index + 1) * SOURCE_SLICE)),
    heading, sourceOnly: true }
}

/** GFM の表だけは行境界で分割し、表頭と解決済み inline token を再利用する。 */
function* tableParts(table: Tokens.Table): Generator<Tokens.Table> {
  const lines = table.raw.trimEnd().split('\n')
  // lexer の将来変更で行対応が不明になった場合、原文を勝手に組み替えない。
  if (lines.length !== table.rows.length + 2 || table.rows.length === 0) { yield table; return }
  const header = lines.slice(0, 2).join('\n') + '\n'
  const headerWeight = tokenWeight({ ...table, rows: [], raw: header })
  if (headerWeight > PAGE_WEIGHT) { yield table; return }
  let start = 0
  while (start < table.rows.length) {
    let end = start
    let weight = headerWeight
    while (end < table.rows.length && end - start < TABLE_ROWS) {
      const nextWeight = lines[end + 2]!.length + 1 + tokenWeight(table.rows[end])
      if (end > start && weight + nextWeight > PAGE_WEIGHT) break
      weight += nextWeight
      end++
    }
    yield { ...table, rows: table.rows.slice(start, end), raw: header + lines.slice(start + 2, end + 2).join('\n') + '\n' }
    start = end
  }
}

/** 全文を一度だけ lex して参照 link を解決し、大きい入力は呼出側で Worker に隔離する。 */
export function markdownPreviewPages(source: string, mode: 'document' | 'reading' = 'document'): MarkdownPreviewPage[] {
  const sourcePages = (): MarkdownPreviewPage[] => Array.from({ length: markdownSourcePageCount(source) }, (_, index) => markdownSourcePage(source, index))
  if (exceedsLexStructureBudget(source)) return sourcePages()
  const pages: MarkdownPreviewPage[] = []
  let tokens: Token[] = []
  let weight = 0
  let heading = ''
  let pageHeading = ''
  const flush = (): boolean => {
    if (!tokens.length) return true
    if (pages.length >= MARKDOWN_PREVIEW_MAX_PAGES) return false
    pages.push({ tokens, source: tokens.map((token) => token.raw).join(''), heading: pageHeading, sourceOnly: false })
    tokens = []
    weight = 0
    return true
  }
  for (const token of marked.lexer(source, { gfm: true, breaks: mode === 'reading' })) {
    const parts = token.type === 'table' ? tableParts(token as Tokens.Table) : [token]
    let partIndex = 0
    for (const part of parts) {
      const continuedTable = partIndex++ > 0
      const nextWeight = tokenWeight(part)
      if (part.type === 'heading') heading = (part as Tokens.Heading).text.slice(0, 160)
      if (nextWeight > PAGE_WEIGHT || part.raw.length > MARKDOWN_PREVIEW_PAGE_CHARACTERS) {
        if (!flush()) return sourcePages()
        for (let index = 0; index < markdownSourcePageCount(part.raw); index++) {
          if (pages.length >= MARKDOWN_PREVIEW_MAX_PAGES) return sourcePages()
          pages.push(markdownSourcePage(part.raw, index, heading))
        }
        continue
      }
      if ((continuedTable || weight && weight + nextWeight > PAGE_WEIGHT) && !flush()) return sourcePages()
      if (!tokens.length || !pageHeading) pageHeading = heading
      tokens.push(part)
      weight += nextWeight
    }
  }
  if (!flush()) return sourcePages()
  // 単頁でも参照定義だけが巨大な文書は原文を無制限に message へ載せない。
  if (pages.length <= 1 && source.length <= MARKDOWN_PREVIEW_PAGE_CHARACTERS) {
    return [{ tokens: pages[0]?.tokens ?? [], source, heading: pages[0]?.heading ?? '', sourceOnly: pages[0]?.sourceOnly ?? false }]
  }
  if (!pages.length) return sourcePages()
  return pages
}
