/** JSON の空白だけを整形する。大きな数値、重複 key、escape と原ファイルは変更しない。 */
export function formatJsonPreview(text: string, filename: string): string {
  if (!/\.json$/i.test(filename)) return text
  try { JSON.parse(text) } catch { return text }
  const tokens = text.match(/"(?:\\.|[^"\\])*"|[^\s{}\[\],:"]+|[{}\[\],:]/g) ?? []
  const chunks: string[] = []
  let depth = 0
  let size = 0
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]!
    let chunk = token
    if (token === '{' || token === '[') {
      depth += 1
      if (depth > 128) return text
      if (tokens[index + 1] !== (token === '{' ? '}' : ']')) chunk += `\n${'  '.repeat(depth)}`
    } else if (token === '}' || token === ']') {
      depth -= 1
      if (tokens[index - 1] !== (token === '}' ? '{' : '[')) chunk = `\n${'  '.repeat(depth)}${token}`
    } else if (token === ',') chunk = `,\n${'  '.repeat(depth)}`
    else if (token === ':') chunk = ': '
    size += chunk.length
    // 深い配列の字下げでプレビュー本文を無制限に膨張させない。
    if (size > 8_000_000) return text
    chunks.push(chunk)
  }
  return chunks.join('')
}
