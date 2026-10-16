import { marked, type Tokens } from 'marked'
import { describe, expect, it } from 'vitest'
import { compactPreviewTables } from '../../src/lib/compactPreviewTables'

describe('compact preview tables', () => {
  it('preserves original coordinates and values without mutating source tokens', () => {
    const tokens = marked.lexer('| Excel row | A | B | C |\n|---|---|---|---|\n| 1 | | | |\n| 3 | | **Title** | |\n| 18 | Company | | |\n\nWarning: static style only.')
    const original = JSON.stringify(tokens)
    const table = compactPreviewTables(tokens)[0] as Tokens.Table
    expect(table.header.map((cell) => cell.text)).toEqual(['Excel row', 'A', 'B'])
    expect(table.rows.map((row) => row.map((cell) => cell.text))).toEqual([['3', '', '**Title**'], ['18', 'Company', '']])
    expect(JSON.stringify(tokens)).toBe(original)
    expect(marked.parser(compactPreviewTables(tokens))).toContain('Warning: static style only.')
    expect(marked.parser(compactPreviewTables(tokens))).toContain('<strong>Title</strong>')
  })
  it('keeps meaningful headers, numeric values, and entirely blank tables', () => {
    const tokens = marked.lexer('| Name | Required value |\n|---|---|\n| A | |\n| 0 | |')
    expect(compactPreviewTables(tokens)[0]).toBe(tokens[0])
    const empty = marked.lexer('| Excel row | A |\n|---|---|\n| 1 | |')
    expect(compactPreviewTables(empty)[0]).toBe(empty[0])
  })
})
