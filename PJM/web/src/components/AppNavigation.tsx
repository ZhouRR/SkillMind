import { useEffect, useRef, useState, type ReactNode } from 'react'

import type { MetaState, ProjectState } from '../appState'
import {
  loadPendingRuns,
  type AuthenticatedUserRecord,
  type ProjectModuleRecord,
} from '../api'
import { useMessages, useUiLanguage } from '../i18n'
import { UI_LANGUAGES, type UiLanguage } from '../lib/i18n/messages'
import { asUiLanguage } from '../lib/i18n/resolve'
import {
  APP_ROUTES,
  routeHref,
  routeUsesModuleFilter,
  type AppRoute,
  type RouteScope,
} from '../lib/routing'
import { ProjectContextSelect } from './PageElements'
import { ROUTE_ICONS } from './routeIcons'

/** 導航徽标が読む待機 Run の上限。件数の桁を抑え、徽标が数字で崩れないようにする。 */
const PENDING_BADGE_LIMIT = 20

/** Product identity、平台/項目二分の主導航、唯一の Project 切替、接続状態を持つ常設 sidebar。 */
export function AppNavigation({ currentRoute, metaState, projectId, projectState, onSelectLanguage, onSelectProject, onSelectModule, activeModuleId, modules, user, onLogout, logoutError }: {
  currentRoute: AppRoute
  metaState: MetaState
  projectId: string
  projectState: ProjectState
  onSelectLanguage: (language: UiLanguage) => void
  onSelectProject: (projectId: string) => void
  onSelectModule: (moduleId: string) => void
  activeModuleId: string
  /** 現在 Project の業務模块。取得は shell(App)が持ち、sidebar は表示だけを担う。 */
  modules: ProjectModuleRecord[]
  user: AuthenticatedUserRecord
  onLogout: () => void
  logoutError: string | null
}) {
  const messages = useMessages()
  const language = useUiLanguage()
  const [pendingCount, setPendingCount] = useState(0)
  const pendingController = useRef<AbortController | null>(null)

  // 応答・承認待ちの件数は導航に常駐させる。待機中の Run は lease も timeout も持たないため、
  // 概览を開かない限り気付かれない待機がそのまま停止になる。取得失敗時は 0 に倒し、
  // 「無い」ではなく「出さない」で誤誘導を避ける。
  useEffect(() => {
    pendingController.current?.abort()
    if (!projectId) {
      setPendingCount(0)
      return
    }
    const controller = new AbortController()
    pendingController.current = controller
    void loadPendingRuns(projectId, PENDING_BADGE_LIMIT, controller.signal)
      .then((items) => {
        if (!controller.signal.aborted) setPendingCount(items.length)
      })
      .catch(() => {
        if (!controller.signal.aborted) setPendingCount(0)
      })
    return () => controller.abort()
  }, [projectId, currentRoute])

  return (
    <aside className="sidebar">
      <a className="brand" href={routeHref('home')} aria-label={messages.nav.brandAriaHome}>
        <span className="brandMark">PM</span>
        <span className="brandName"><strong>ProjectMind</strong><small>{messages.nav.brandTagline}</small></span>
      </a>
      <nav className="sideNav" aria-label={messages.nav.mainNavAria}>
        <span className="navGroupLabel">{messages.nav.platformGroup}</span>
        <NavGroup badges={{ home: pendingCount }} currentRoute={currentRoute} scope="platform" />
        {/* Project 切替は sidebar のここが唯一の入口。select の label が「当前项目」分組の見出しを兼ねる。 */}
        <div className="sideNavProject">
          <ProjectContextSelect
            label={messages.nav.currentProject}
            projectId={projectId}
            projectState={projectState}
            onSelect={onSelectProject}
          />
        </div>
        {/* 子菜单は項目の主画面である工作空间へ入れ子にする。選択の遷移先も同じ画面なので、
            強調が親と子で割れない(別画面へ挂けると、選んだ瞬間に強調だけが飛んで見える)。 */}
        <NavGroup
          currentRoute={currentRoute}
          scope="project"
          subNavFor="workspace"
          subNav={projectId !== '' && modules.length > 0 && (
            <ModuleNavList
              activeModuleId={routeUsesModuleFilter(currentRoute) ? activeModuleId : null}
              modules={modules}
              onSelect={onSelectModule}
            />
          )}
        />
      </nav>
      <div className="sidebarFooter">
        <ServiceStatus state={metaState} />
        {/* 言語切替は認証済み sidebar だけに置き、選択は user preference として保存される。 */}
        <label className="sidebarLanguage">
          <span>{messages.language.label}</span>
          <select
            value={language}
            onChange={(event) => {
              const next = asUiLanguage(event.target.value)
              if (next) onSelectLanguage(next)
            }}
          >
            {UI_LANGUAGES.map((candidate) => (
              <option key={candidate} value={candidate}>{messages.language.names[candidate]}</option>
            ))}
          </select>
        </label>
        <div className="sidebarUser">
          <span>{user.display_name}</span>
          <small>{user.system_role} · {user.email}</small>
        </div>
        {logoutError && <small className="sidebarError" role="alert">{logoutError}</small>}
        <button className="sidebarLogout" onClick={onLogout} type="button">{messages.nav.logout}</button>
        <a className="sidebarLink" href={`${import.meta.env.BASE_URL}api/docs`}>{messages.nav.apiDocs}</a>
      </div>
    </aside>
  )
}

/** 指定 scope の route 群を宣言順で描画する。subNavFor の直後に子菜单を差し込める。 */
function NavGroup({ currentRoute, scope, subNavFor, subNav, badges }: {
  currentRoute: AppRoute
  scope: RouteScope
  subNavFor?: AppRoute
  subNav?: ReactNode
  /** Route ごとの件数徽标。0 は描画しない（「0 件」を出すと常時点灯して意味が薄れる）。 */
  badges?: Partial<Record<AppRoute, number>>
}) {
  const messages = useMessages()
  return (
    <>
      {APP_ROUTES.filter((entry) => entry.scope === scope).map(({ route }) => {
        const badge = badges?.[route] ?? 0
        return (
          <div className="sideNavEntry" key={route}>
            <a
              aria-current={currentRoute === route ? 'page' : undefined}
              href={routeHref(route)}
            >
              {ROUTE_ICONS[route]}
              <span>{messages.routes[route].label}</span>
              {badge > 0 && (
                <span className="navBadge" title={messages.pending.title}>{badge}</span>
              )}
            </a>
            {route === subNavFor && subNav}
          </div>
        )
      })}
    </>
  )
}

/** 業務模块の子菜单。module 名だけを縦に並べ、常にどれか一つが現在の作業範囲になる。
 *
 * 「全部」入口は置かない。task が増えるほど全件表示は読めなくなり、模块で見るという
 * 本来の導線と二重になるため。module を持たない Project ではこの子菜单自体を出さない。
 *
 * 絞り込みは任务中心と工作空间の双方に効く（どちらも同じ module 選択を読む）ので、
 * 強調も両画面で続ける。遷移先の画面で強調が消えると、選択が失われたように見える。 */
export function ModuleNavList({ modules, activeModuleId, onSelect }: {
  modules: ProjectModuleRecord[]
  /** 絞り込みが効かない画面では null(未強調)。 */
  activeModuleId: string | null
  onSelect: (moduleId: string) => void
}) {
  const messages = useMessages()
  return (
    <div className="sideNavSub" role="group" aria-label={messages.nav.projectModulesAria}>
      {modules.map((module) => (
        <button
          aria-current={activeModuleId === module.module_id ? 'true' : undefined}
          className="sideNavSubItem"
          key={module.module_id}
          title={module.description || module.name}
          type="button"
          onClick={() => onSelect(module.module_id)}
        >
          {module.name}
        </button>
      ))}
    </div>
  )
}

/** API metadata の読み込み結果を接続状態 indicator として常時表示する。 */
function ServiceStatus({ state }: { state: MetaState }) {
  const messages = useMessages()
  const [dotClass, label] = state.status === 'loading'
    ? ['dotPending', messages.service.connecting]
    : state.status === 'error'
      ? ['dotError', messages.service.failed]
      : ['dotReady', messages.service.ok(state.meta.version)]
  // 狭幅では文言を隠して状態点だけ残すため、aria-label で読み上げ名を常に保持する。
  return (
    <div className="serviceStatus" aria-label={label} title={label}>
      <span className={`statusDot ${dotClass}`} />
      <span className="serviceStatusText">{label}</span>
    </div>
  )
}
