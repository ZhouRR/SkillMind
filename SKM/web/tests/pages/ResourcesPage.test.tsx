import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ResourcesPage } from '../../src/pages/ResourcesPage'
import { DEMO_PROJECT as PROJECT } from '../fixtures'

/** 既定言語(zh)の管理画面を静的描画する。effect は走らないため一覧は空のまま。 */
function page(projectId: string = PROJECT.project_id): string {
  return renderToStaticMarkup(
    <ResourcesPage csrfToken={'s'.repeat(32)} projectId={projectId} />,
  )
}

describe('ResourcesPage guided layout', () => {
  it('omits write access and the policy tab when deferred features are disabled', () => {
    const html = renderToStaticMarkup(<ResourcesPage csrfToken="fixture" projectId={PROJECT.project_id} deferredFeaturesEnabled={false} />)
    expect(html).toContain('接入外部系统')
    expect(html).not.toContain('name="connect-access"')
    expect(html).not.toMatch(/role="tab"[^>]*>预授权</)
  })

  it('asks for a project before showing any configuration', () => {
    const html = page('')

    expect(html).toContain('请先在侧栏选择项目。')
    expect(html).not.toContain('接入外部系统')
  })

  it('renders the single connect form with structured Redmine inputs by default', () => {
    const html = page()

    expect(html).toContain('接入外部系统')
    expect(html).toContain('系统类型')
    expect(html).toContain('Redmine 地址')
    expect(html).toContain('工单范围')
    expect(html).toContain('不限（以凭据可见范围为界）')
    expect(html).toContain('指定工单列表')
    expect(html).toContain('访问权限')
    expect(html).toContain('只读 + 允许提议修改')
    expect(html).toContain('凭据')
    // 既定は平台托管:明文入力(password)を出し、locator 入力は出さない。
    expect(html).toContain('凭据内容')
    expect(html).toContain('type="password"')
    expect(html).not.toContain('存放位置')
  })

  it('defaults to the unrestricted issue range and hides the enumeration inputs', () => {
    // 既定は「不限」。列挙 textarea は指定 mode を選んだ時だけ現れる。
    const html = page()

    expect(html).not.toContain('工单编号（每行一个）')
  })

  it('no longer asks for raw JSON or capability ID strings', () => {
    const html = page()

    // 裸 JSON textarea と capability 手入力の廃止がこの改修の核心。復活を回帰で防ぐ。
    expect(html).not.toContain('JSON')
    expect(html).not.toContain('能力列表')
  })

  it('hides field-level write scope until change proposals are allowed', () => {
    // 既定は只読档位なので、write 専用の field 档位選択は最初から見せない。
    const html = page()

    expect(html).not.toContain('可修改字段')
    expect(html).not.toContain('任意字段')
    expect(html).not.toContain('status_id')
  })

  it('splits credentials, bindings, and preauthorizations into tabs', () => {
    const html = page()

    // 4 区分(连接/凭据/绑定/预授权)は tab で切り替える。旧 details 折り畳みは廃止。
    expect(html).toContain('role="tablist"')
    expect(html).toContain('role="tabpanel"')
    expect(html).not.toContain('resourceAdvanced')
    // 非活性 tab の中身も hidden で DOM に残すため、binding/policy の内容は描画される。
    expect(html).toContain('默认绑定与范围收窄')
    expect(html).toContain('低风险写入预授权')
    // write 可能な Integration が無い間は、事前許可 form ではなく導線の説明を出す。
    expect(html).toContain('没有允许提议修改的 Redmine 集成')
  })

  it('shows the empty-state guide for the connected system list', () => {
    const html = page()

    expect(html).toContain('已接入系统')
    expect(html).toContain('尚未连接外部系统')
  })

  it('keeps each section as a list and hosts the creation forms in always-mounted modals', () => {
    const html = page()

    // 一覧が主役、新規作成 form は常時 mount + hidden の共通弹窗へ(閉じた状態で描画される)。
    expect(html).toContain('modalOverlay')
    expect(html).toContain('hidden=""')
    // 各区分の新規作成導線。
    expect(html).toContain('新建绑定')
    expect(html).toContain('新建预授权')
    expect(html).toContain('登记凭据位置')
  })
})
