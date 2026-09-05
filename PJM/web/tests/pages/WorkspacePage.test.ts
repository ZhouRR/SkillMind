import { describe, expect, it } from 'vitest'

import { shouldRefreshRunHistory } from '../../src/pages/WorkspacePage'

describe('Run history refresh decision', () => {
  it('refreshes when an active Run reaches a terminal status', () => {
    /** 新しい Result summary を一覧へ反映する終態遷移を検証する。 */
    expect(shouldRefreshRunHistory('RUNNING', 'SUCCEEDED')).toBe(true)
  })

  it('does not refresh when replaying an already terminal historical Run', () => {
    /** History 選択時の不要な再取得と一覧の表示跳ねを防ぐ。 */
    expect(shouldRefreshRunHistory('SUCCEEDED', 'SUCCEEDED')).toBe(false)
  })

  it('does not refresh while the Run remains active', () => {
    /** 通常の snapshot 更新で history API を連続呼び出ししないことを守る。 */
    expect(shouldRefreshRunHistory('PREPARING', 'RUNNING')).toBe(false)
  })
})
