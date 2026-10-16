import type { Token, Tokens } from 'marked'

/** 表示 token だけから空白行列を除く。原文・行番号・非空セルと周辺の注記は変更しない。 */
export function compactPreviewTables(tokens: Token[]): Token[] {
  return tokens.map((token) => token.type === 'table' ? compactTable(token as Tokens.Table) : token)
}

/** 変換表の列見出しは座標なので、本文が空の列を省ける。一般表の意味を持つ見出しは残す。 */
function compactTable(table: Tokens.Table): Tokens.Table {
  const indexed = table.header[0]?.text === 'Excel row'
  const rows = table.rows.filter((row) => row.some((cell, index) => (!indexed || index > 0) && cell.text.trim()))
  if (rows.length === 0) return table
  const columns = table.header.flatMap((cell, index) => (indexed && index === 0)
    || (!indexed && cell.text.trim()) || rows.some((row) => row[index]?.text.trim()) ? [index] : [])
  if (rows.length === table.rows.length && columns.length === table.header.length) return table
  return { ...table, header: columns.map((index) => table.header[index]!),
    align: columns.map((index) => table.align[index] ?? null),
    rows: rows.map((row) => columns.map((index) => row[index]!)) }
}
