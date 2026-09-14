import { describe, expect, it } from 'vitest'
import { formatJsonPreview } from '../../src/lib/jsonPreview'

describe('JSON preview whitespace formatting', () => {
  it('indents objects, arrays and empty containers', () => {
    expect(formatJsonPreview('{"cases":[{"id":1},{}],"empty":[]}', 'output/result.JSON'))
      .toBe('{\n  "cases": [\n    {\n      "id": 1\n    },\n    {}\n  ],\n  "empty": []\n}')
  })

  it('preserves numeric precision, duplicate keys and literal escapes', () => {
    const text = String.raw`{"id":9007199254740993,"id":1e400,"v":-0,"text":"a,{}[\"x\"]:\n\u65e5"}`
    expect(formatJsonPreview(text, 'result.json')).toBe(String.raw`{
  "id": 9007199254740993,
  "id": 1e400,
  "v": -0,
  "text": "a,{}[\"x\"]:\n\u65e5"
}`)
  })

  it.each(['null', 'true', '"日本語"', '123', '{}', '[]'])('supports scalar/empty JSON: %s', (text) => {
    expect(formatJsonPreview(text, 'result.json')).toBe(text)
  })

  it.each(['{"bad":}', 'plain text', '{"trailing":1,}'])('keeps malformed JSON readable: %s', (text) => {
    expect(formatJsonPreview(text, 'result.json')).toBe(text)
  })

  it('leaves ordinary text and excessive nesting unchanged', () => {
    const text = '{"a":1}'
    expect(formatJsonPreview(text, 'result.txt')).toBe(text)
    const deep = '['.repeat(129) + '0' + ']'.repeat(129)
    expect(formatJsonPreview(deep, 'result.json')).toBe(deep)
  })
})
