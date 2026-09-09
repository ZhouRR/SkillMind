import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

import type { MetaState, ProjectState } from '../appState'
import {
  ApiProblemError,
  loadPendingRuns,
  type AuthenticatedUserRecord,
  type ProjectModuleRecord,
  type ProjectRecord,
} from '../api'
import { useMessages, useUiLanguage } from '../i18n'
import { UI_LANGUAGES, type UiLanguage } from '../lib/i18n/messages'
import { asUiLanguage } from '../lib/i18n/resolve'
import {
  APP_ROUTES,
  routeHref,
  routeHrefWithProject,
  routeUsesModuleFilter,
  type AppRoute,
  type RouteScope,
} from '../lib/routing'
import { ProjectContextSelect } from './PageElements'
import { ROUTE_ICONS } from './routeIcons'

/** 導航徽标が読む待機 Run の上限。件数の桁を抑え、徽标が数字で崩れないようにする。 */
const PENDING_BADGE_LIMIT = 20

/** CSS と同じ閾値を使い、狭幅で隠した操作を tab 順序からも外す。 */
const COMPACT_NAV_QUERY = '(max-width: 960px)'

/** Product identity、主導航、唯一の Project 切替、接続状態を幅に応じて披露する。 */
export function AppNavigation({ currentRoute, metaState, projectId, projectState, currentProject, pendingProjectId = projectId, projectHash, onRefreshProjects, onSelectLanguage, onSelectProject, onSelectModule, activeModuleId, modules, user, onLogout, onSessionEnded, logoutError, logoutPending = false }: {
  currentRoute: AppRoute
  metaState: MetaState
  projectId: string
  projectState: ProjectState
  /** 一覧状態を書き換えず、認可済みの精確 Project を selector に補う。 */
  currentProject?: ProjectRecord | null
  /** URL の表示対象と違い、待機 Run の読取が許可済みの Project だけを渡す。 */
  pendingProjectId?: string
  /** 空・重複を含む明示 Project query を勝手に修正せず、他画面にも保持する。 */
  projectHash?: string
  onRefreshProjects?: () => void
  onSelectLanguage: (language: UiLanguage) => void
  onSelectProject: (projectId: string) => void
  onSelectModule: (moduleId: string) => void
  activeModuleId: string
  /** 現在 Project の業務模块。取得は shell(App)が持ち、sidebar は表示だけを担う。 */
  modules: ProjectModuleRecord[]
  user: AuthenticatedUserRecord
  onLogout: () => void
  onSessionEnded?: () => void
  logoutError: string | null
  logoutPending?: boolean
}) {
  const messages = useMessages()
  const language = useUiLanguage()
  const [pendingCount, setPendingCount] = useState(0)
  const pendingController = useRef<AbortController | null>(null)
  const sessionEnded = useRef(onSessionEnded)
  const menuId = useId()
  const [compact, setCompact] = useState(() => (
    typeof window !== 'undefined' && window.matchMedia(COMPACT_NAV_QUERY).matches
  ))
  const [menuOpen, setMenuOpen] = useState(false)
  const menuToggle = useRef<HTMLButtonElement>(null)
  const menuPanel = useRef<HTMLDivElement>(null)
  const brandLink = useRef<HTMLAnchorElement>(null)
  const lastFocused = useRef<Element | null>(null)
  sessionEnded.current = onSessionEnded

  /** Run/Task は引き継がず、Project の明示目標だけを route 間で保つ。 */
  function navigationHref(route: AppRoute): string {
    return projectHash === undefined
      ? routeHref(route, projectId)
      : routeHrefWithProject(route, projectHash, projectId)
  }

  /** 披露を閉じた後も、隠れた control へ keyboard focus を残さない。 */
  function closeMenu(): void {
    if (!compact) return
    setMenuOpen(false)
    menuToggle.current?.focus()
  }

  useLayoutEffect(() => {
    const query = window.matchMedia(COMPACT_NAV_QUERY)
    /** CSS が先に focused control を隠して body へ戻す場合も、直前の操作位置を失わない。 */
    function rememberFocus(event: FocusEvent): void {
      if (event.target instanceof Element
        && event.target !== window.document.body
        && event.target !== window.document.documentElement) {
        lastFocused.current = event.target
      }
    }
    /** 回転・resize で配置が切り替わった場合も、不可視領域へ焦点を残さない。 */
    function updateLayout(): void {
      const active = window.document.activeElement
      const focused = active === window.document.body ? lastFocused.current : active
      if (query.matches && focused && menuPanel.current?.contains(focused)) {
        menuToggle.current?.focus()
      } else if (!query.matches && focused === menuToggle.current) {
        brandLink.current?.focus()
      }
      setCompact(query.matches)
      setMenuOpen(false)
    }
    lastFocused.current = window.document.activeElement
    window.document.addEventListener('focusin', rememberFocus)
    updateLayout()
    query.addEventListener('change', updateLayout)
    return () => {
      query.removeEventListener('change', updateLayout)
      window.document.removeEventListener('focusin', rememberFocus)
    }
  }, [])

  useEffect(() => {
    if (!compact || !menuOpen) return
    /** 披露は modal ではないため Tab を閉じ込めず、Escape だけを閉操作にする。 */
    function handleEscape(event: KeyboardEvent): void {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setMenuOpen(false)
      menuToggle.current?.focus()
    }
    window.addEventListener('keydown', handleEscape)
    return () => window.removeEventListener('keydown', handleEscape)
  }, [compact, menuOpen])

  // 応答・承認待ちの件数は導航に常駐させる。待機中の Run は lease も timeout も持たないため、
  // 概览を開かない限り気付かれない待機がそのまま停止になる。取得失敗時は 0 に倒し、
  // 「無い」ではなく「出さない」で誤誘導を避ける。
  // layout cleanup で旧読取を無効化し、対象切替と passive effect の隙間に来る 401 も捨てる。
  useLayoutEffect(() => {
    pendingController.current?.abort()
    setPendingCount(0)
    if (!pendingProjectId) {
      return
    }
    const controller = new AbortController()
    pendingController.current = controller
    void loadPendingRuns(pendingProjectId, PENDING_BADGE_LIMIT, controller.signal)
      .then((items) => {
        if (!controller.signal.aborted) setPendingCount(items.length)
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setPendingCount(0)
        if (error instanceof ApiProblemError && error.status === 401) sessionEnded.current?.()
      })
    return () => controller.abort()
  }, [pendingProjectId, currentRoute])

  return (
    <aside className="sidebar">
      <div className="sidebarHeader">
        <a className="brand" href={navigationHref('home')} aria-label={messages.nav.brandAriaHome} onClick={closeMenu} ref={brandLink}>
          <span className="brandMark">PM</span>
          <span className="brandName"><strong>ProjectMind</strong><small>{messages.nav.brandTagline}</small></span>
        </a>
        <button
          aria-controls={menuId}
          aria-expanded={compact && menuOpen}
          aria-label={menuOpen ? messages.nav.closeMenu : messages.nav.openMenu}
          className="sidebarMenuToggle"
          onClick={() => setMenuOpen((open) => !open)}
          ref={menuToggle}
          type="button"
        >
          <svg aria-hidden="true" viewBox="0 0 24 24"><path d={menuOpen ? 'm6 6 12 12M6 18 18 6' : 'M4 6h16M4 12h16M4 18h16'} /></svg>
          <span>{messages.nav.menuLabel}</span>
        </button>
      </div>
      <div className="navigationPanel" hidden={compact && !menuOpen} id={menuId} ref={menuPanel}>
        <nav className="sideNav" aria-label={messages.nav.mainNavAria}>
          <span className="navGroupLabel">{messages.nav.platformGroup}</span>
          <NavGroup badges={{ home: pendingCount }} currentRoute={currentRoute} hrefFor={navigationHref} onNavigate={closeMenu} scope="platform" />
          {/* Project 切替は sidebar のここが唯一の入口。select の label が「当前项目」分組の見出しを兼ねる。 */}
          <div className="sideNavProject">
            <ProjectContextSelect
              label={messages.nav.currentProject}
              projectId={projectId}
              projectState={projectState}
              currentProject={currentProject}
              onRefresh={onRefreshProjects}
              onSelect={(selected) => { closeMenu(); onSelectProject(selected) }}
            />
          </div>
          {/* 子菜单は項目の主画面である工作空间へ入れ子にする。選択の遷移先も同じ画面なので、
              強調が親と子で割れない(別画面へ挂けると、選んだ瞬間に強調だけが飛んで見える)。 */}
          <NavGroup
            currentRoute={currentRoute}
            hrefFor={navigationHref}
            onNavigate={closeMenu}
            scope="project"
            subNavFor="workspace"
            subNav={projectId !== '' && modules.length > 0 && (
              <ModuleNavList
                activeModuleId={routeUsesModuleFilter(currentRoute) ? activeModuleId : null}
                modules={modules}
                onSelect={(selected) => { closeMenu(); onSelectModule(selected) }}
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
          <button className="sidebarLogout" disabled={logoutPending} onClick={onLogout} type="button">{messages.nav.logout}</button>
          <a className="sidebarLink" href={`${import.meta.env.BASE_URL}api/docs`} onClick={closeMenu}>{messages.nav.apiDocs}</a>
        </div>
      </div>
    </aside>
  )
}

/** 指定 scope の route 群を宣言順で描画する。subNavFor の直後に子菜单を差し込める。 */
function NavGroup({ currentRoute, hrefFor, scope, subNavFor, subNav, badges, onNavigate }: {
  currentRoute: AppRoute
  hrefFor: (route: AppRoute) => string
  scope: RouteScope
  subNavFor?: AppRoute
  subNav?: ReactNode
  /** Route ごとの件数徽标。0 は描画しない（「0 件」を出すと常時点灯して意味が薄れる）。 */
  badges?: Partial<Record<AppRoute, number>>
  onNavigate: () => void
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
              href={hrefFor(route)}
              onClick={onNavigate}
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
  // 状態の意味を色だけに頼らず、披露内でも文字と読み上げ名を保持する。
  return (
    <div className="serviceStatus" aria-label={label} title={label}>
      <span className={`statusDot ${dotClass}`} />
      <span className="serviceStatusText">{label}</span>
    </div>
  )
}
