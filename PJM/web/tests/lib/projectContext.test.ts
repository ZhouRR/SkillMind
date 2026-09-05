import { describe, expect, it } from 'vitest'

import type { ProjectRecord } from '../../src/api'
import { resolveProjectSelection } from '../../src/lib/projectContext'
import { DEMO_PROJECT } from '../fixtures'

/** 参照可能な 2 件目の Project。既定選択が先頭を採ることの確認に使う。 */
const OTHER_PROJECT: ProjectRecord = {
  ...DEMO_PROJECT,
  project_id: '00000000-0000-4000-8000-000000000011',
  key: 'platform-team',
  name: 'Platform Team',
}

const UNKNOWN_PROJECT_ID = '00000000-0000-4000-8000-0000000000ff'

describe('resolveProjectSelection', () => {
  it('keeps the project named by the URL when the actor can access it', () => {
    /** 明示された link 先を保存済み選択より優先することを守る。 */
    const selection = resolveProjectSelection(
      [DEMO_PROJECT, OTHER_PROJECT],
      OTHER_PROJECT.project_id,
      DEMO_PROJECT.project_id,
    )

    expect(selection).toBe(OTHER_PROJECT.project_id)
  })

  it('falls back to the saved preference when the URL names no project', () => {
    const selection = resolveProjectSelection(
      [DEMO_PROJECT, OTHER_PROJECT],
      null,
      OTHER_PROJECT.project_id,
    )

    expect(selection).toBe(OTHER_PROJECT.project_id)
  })

  it('selects the first accessible project when nothing is remembered', () => {
    /** 初回ログインでも必ず一つ選ぶ。未選択のままでは全画面が「先に選んでください」で止まる。 */
    const selection = resolveProjectSelection([DEMO_PROJECT, OTHER_PROJECT], null, null)

    expect(selection).toBe(DEMO_PROJECT.project_id)
  })

  it('selects the first project when the saved preference is no longer accessible', () => {
    /** 保存済み選択が廃止・越権になっても、画面を空のまま残さない。 */
    const selection = resolveProjectSelection([DEMO_PROJECT], null, UNKNOWN_PROJECT_ID)

    expect(selection).toBe(DEMO_PROJECT.project_id)
  })

  it('falls back without ceremony when the URL names an inaccessible project', () => {
    /** 失効した link で警告を出しても、hash と sidebar が示す現在 Project を繰り返すだけになる。 */
    const selection = resolveProjectSelection(
      [DEMO_PROJECT],
      UNKNOWN_PROJECT_ID,
      DEMO_PROJECT.project_id,
    )

    expect(selection).toBe(DEMO_PROJECT.project_id)
  })

  it('selects nothing when no project is accessible', () => {
    /** 選べる Project が無い状態は選択欄の空文言だけが説明する。 */
    const selection = resolveProjectSelection([], UNKNOWN_PROJECT_ID, DEMO_PROJECT.project_id)

    expect(selection).toBe('')
  })
})
