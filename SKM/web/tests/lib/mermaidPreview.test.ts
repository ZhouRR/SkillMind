import { marked, type Tokens } from 'marked'
import { describe, expect, it } from 'vitest'
import { MERMAID_MAX_CHARACTERS, MERMAID_MAX_EDGES, MERMAID_MAX_NODES,
  mermaidCodeHtml, mermaidCodeTokens, safeMermaidFlowchart } from '../../src/lib/mermaidPreview'

/** code token の同一性を保って本物の marked parser を通す。 */
function code(source: string): Tokens.Code { return mermaidCodeTokens(marked.lexer(`\`\`\`mermaid\n${source}\n\`\`\``))[0]! }
const labels = { loading: 'Loading diagram', failed: 'Diagram unavailable; showing source', title: 'Flowchart' }

describe('bounded Mermaid flowchart preflight', () => {
  it('canonicalizes IDs and supports chains, directions, plain labels, common shapes and edge labels', () => {
    const source = 'graph LR; A[开始] --> B{通过?}; B -->|是| C((完成)); B -.-> D([再次检查])'
    const safe = safeMermaidFlowchart(source)
    expect(safe).toContain('flowchart LR')
    expect(safe).toContain('n0["开始"] --> n1{"通过?"}')
    expect(safe).toContain('n1 -->|"是"| n2(("完成"))')
    expect(safe).not.toContain('A[')
  })
  it('checks a bare generated chain with the real Mermaid parser without a browser DOM', async () => {
    // Node では label 解析に必要な DOMPurify DOM がないため、実 parser の検証は裸の chain に限定する。
    const { default: mermaid } = await import('mermaid')
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', htmlLabels: false })
    await expect(mermaid.parse('flowchart LR\nn0 --> n1 --> n2')).resolves.toMatchObject({ diagramType: 'flowchart-v2' })
  })
  it.each([
    'sequenceDiagram\nA->>B: Hello', 'A→B→C', 'flowchart LR\nA[unterminated',
    '%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\nA-->B',
    '---\nconfig:\n securityLevel: loose\n---\nflowchart LR\nA-->B',
    'flowchart LR\nA-->B\nclick A "https://example.test"',
    'flowchart LR\nA[<img src="https://example.test">]-->B',
    'flowchart LR\nA[&lt;img&gt;]-->B', 'flowchart LR\nA[fa:fa-car]-->B',
    'flowchart LR\nA["`**markdown**`"]-->B', 'flowchart LR\nA["$$x$$"]-->B',
    'flowchart LR\nA@{ img: "https://example.test/image" }',
    'flowchart LR\nA-->B\nstyle A fill:url(https://example.test)',
    'flowchart LR\nA-->B\nclassDef x fill:red', 'flowchart LR\nA:::x-->B',
    'flowchart LR\nA-->B\nlinkStyle 0 stroke:red', 'flowchart LR\nsubgraph Other\nA\nend',
  ])('rejects unsupported or resource-bearing DSL without passing it to Mermaid: %s', (source) => {
    expect(safeMermaidFlowchart(source)).toBeNull()
  })
  it('enforces source, node and edge budgets before rendering', () => {
    expect(safeMermaidFlowchart('flowchart LR\nA[' + 'x'.repeat(MERMAID_MAX_CHARACTERS) + ']')).toBeNull()
    expect(safeMermaidFlowchart('flowchart LR\n' + Array.from({ length: MERMAID_MAX_NODES + 1 }, (_, i) => `A${i}`).join('\n'))).toBeNull()
    expect(safeMermaidFlowchart('flowchart LR\n' + 'A-->B\n'.repeat(MERMAID_MAX_EDGES + 1))).toBeNull()
    expect(safeMermaidFlowchart('flowchart LR\n' + 'A-->B\n'.repeat(MERMAID_MAX_EDGES))).not.toBeNull()
  })
})

describe('Mermaid fenced code rendering boundary', () => {
  it('only selects explicit mermaid fences, including nested quotes and tilde fences', () => {
    const tokens = marked.lexer('A→B→C\n\n```text\nA-->B\n```\n\n<pre><code class="language-mermaid">A-->B</code></pre>\n\n> ~~~mermaid\n> flowchart LR\n> A-->B\n> ~~~')
    const diagrams = mermaidCodeTokens(tokens)
    expect(diagrams).toHaveLength(1)
    expect(diagrams[0]!.text).toContain('flowchart LR')
  })
  it('preserves unsafe source as escaped text with explicit failure, without executable markup', () => {
    const token = code('flowchart LR\nA[<img onerror="alert(1)">]')
    const html = mermaidCodeHtml(token, new Map([[token, { status: 'failed' }]]), 'reading', labels)
    expect(html).toContain(labels.failed)
    expect(html).toContain('&lt;img onerror=&quot;')
    expect(html).not.toContain('<img')
    expect(mermaidCodeHtml({ ...token, lang: 'text' }, new Map(), 'reading', labels)).toBe(false)
  })
  it('isolates generated CSS in reading mode and preserves per-token diagram identity', () => {
    const first = code('flowchart LR\nA-->B'); const second = code(first.text)
    const previews = new Map([[first, { status: 'ready' as const, document: '<style>body{color:red}</style><svg id="one"/>',
      svg: '<svg id="one"/>', height: 200 }]])
    const html = mermaidCodeHtml(first, previews, 'reading', labels)
    expect(html).toContain('sandbox=""')
    expect(html).toContain('referrerpolicy="no-referrer"')
    expect(html).toContain('srcdoc="&lt;style&gt;')
    expect(html).not.toContain('<style>')
    expect(mermaidCodeHtml(second, previews, 'reading', labels)).toContain(labels.loading)
    expect(mermaidCodeHtml(first, previews, 'document', labels)).toContain('<svg id="one"/>')
    expect(mermaidCodeHtml(first, previews, 'document', labels)).toContain('max-height:380px;overflow:auto;background:transparent')
  })
})
