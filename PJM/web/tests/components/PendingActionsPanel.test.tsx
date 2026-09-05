import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { PENDING_RUN_STATUSES } from '../../src/api'
import { PendingActionsPanel } from '../../src/components/PendingActionsPanel'

describe('PendingActionsPanel', () => {
  it('defines "waiting for you" as exactly the two blocking statuses', () => {
    // ここに状態を足し引きすると「対応待ち」の意味が静かに変わる。契約値そのものを固定する。
    expect([...PENDING_RUN_STATUSES]).toEqual(['WAITING_FOR_INPUT', 'WAITING_FOR_APPROVAL'])
  })

  it('asks for a project instead of claiming there is nothing to do', () => {
    // 未選択で「対応待ちなし」と出すと、実際には溜まっているのに片付いて見える。
    const html = renderToStaticMarkup(<PendingActionsPanel projectId="" />)

    expect(html).toContain('选择项目后显示需要你处理的执行')
    expect(html).not.toContain('没有需要你回答或批准的执行')
  })

  it('renders the panel heading even before the first fetch resolves', () => {
    const html = renderToStaticMarkup(
      <PendingActionsPanel projectId="00000000-0000-4000-8000-000000000020" />,
    )

    expect(html).toContain('待你处理')
  })
})
