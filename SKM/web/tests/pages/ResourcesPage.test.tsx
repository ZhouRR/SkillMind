import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ResourcesPage } from '../../src/pages/ResourcesPage'
import { DEMO_PROJECT as PROJECT } from '../fixtures'

/** 既定言語(zh)の管理画面を静的描画する。effect は走らないため一覧の読取状態は未確認のまま。 */
function page(projectId: string = PROJECT.project_id): string {
  return renderToStaticMarkup(
    <ResourcesPage csrfToken={'s'.repeat(32)} projectId={projectId} />,
  )
}

describe('ResourcesPage guided layout', () => {
  it('keeps native resource access while hiding deferred policy tabs', () => {
    const html = renderToStaticMarkup(<ResourcesPage csrfToken="fixture" projectId={PROJECT.project_id} deferredFeaturesEnabled={false} />)
    expect(html).toContain('添加连接')
    expect(html).toContain('name="connect-access"')
    expect(html).not.toMatch(/role="tab"[^>]*>自动批准规则</)
  })

  it('asks for a project before showing any configuration', () => {
    const html = page('')

    expect(html).toContain('请先在侧栏选择项目。')
    expect(html).not.toContain('添加连接')
  })

  it('renders the single connect form with generic HTTP inputs by default', () => {
    const html = page()

    expect(html).toContain('添加连接')
    expect(html).toContain('系统类型')
    expect(html).toContain('HTTP API 地址')
    expect(html).toContain('允许访问的 API 路径')
    expect(html).toContain('认证方式')
    expect(html).toContain('访问权限')
    expect(html).toContain('只读 + 允许提议修改')
    expect(html).toContain('凭据')
    // 既定は平台托管:明文入力(password)を出し、locator 入力は出さない。
    expect(html).toContain('API Key')
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

  it('keeps optional credential, binding, and policy tabs inside advanced settings', () => {
    const html = page()

    // 通常画面は認証情報と権限に絞り、任意の詳細設定を disclosure 内へ集約する。
    expect(html).toContain('role="tablist"')
    expect(html).toContain('role="tabpanel"')
    expect(html).toContain('resourceAdvanced')
    // 非活性 tab の中身も hidden で DOM に残すため、binding/policy の内容は描画される。
    expect(html).toContain('任务连接设置')
    expect(html).toContain('低风险写入预授权')
    // 読取前に「書込可能な接続が無い」とは判断しない。
    expect(html).not.toContain('没有允许提议修改的 Redmine 集成')
  })

  it('shows initial loading without claiming that the connection list is empty', () => {
    const html = page()

    expect(html).toContain('连接与权限')
    expect(html).toContain('正在读取资源配置…')
    expect(html).not.toContain('尚未添加配置')
    expect(html).toContain('aria-busy="true"')
  })

  it('keeps each section as a list and hosts the creation forms in always-mounted modals', () => {
    const html = page()

    // 一覧が主役、新規作成 form は常時 mount + hidden の共通弹窗へ(閉じた状態で描画される)。
    expect(html).toContain('modalOverlay')
    expect(html).toContain('hidden=""')
    // 各区分の新規作成導線。
    expect(html).toContain('指定任务连接')
    expect(html).toContain('新建预授权')
    expect(html).toContain('登记凭据位置')
  })
})
