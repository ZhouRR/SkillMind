import { useEffect, useState, type ReactNode } from 'react'

import {
  ApiProblemError,
  loadAuthSession,
  loadMeta,
  loadProjectModules,
  loadProjectPreference,
  loadProjects,
  loadUiLanguage,
  logout,
  saveProjectPreference,
  saveUiLanguage,
  type AuthSessionRecord,
  type ProjectModuleRecord,
  type ProjectRecord,
} from './api'
import { type MetaState, type ProjectState } from './appState'
import { AppNavigation } from './components/AppNavigation'
import { LanguageProvider } from './i18n'
import { MESSAGES, type UiLanguage } from './lib/i18n/messages'
import { resolveUiLanguage } from './lib/i18n/resolve'
import { resolveProjectSelection } from './lib/projectContext'
import { DocumentsPage } from './pages/DocumentsPage'
import { HomePage } from './pages/HomePage'
import { HistoryPage } from './pages/HistoryPage'
import { LoginPage } from './pages/LoginPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { ResourcesPage } from './pages/ResourcesPage'
import { SkillsPage } from './pages/SkillsPage'
import { TasksPage } from './pages/TasksPage'
import { WorkspacePage } from './pages/WorkspacePage'
import {
  APP_ROUTES,
  projectIdFromHash,
  routeContextFromHash,
  routeFromHash,
  routeHref,
  routeUsesModuleFilter,
  type AppRoute,
} from './lib/routing'
import './styles.css'

/** ProjectMind の画面選択と共有 Project context を管理する application shell。 */
export function App() {
  const [route, setRoute] = useState<AppRoute>(() => routeFromHash(window.location.hash))
  const [projectId, setProjectId] = useState('')
  const [metaState, setMetaState] = useState<MetaState>({ status: 'loading' })
  const [authState, setAuthState] = useState<AuthState>({ status: 'loading' })
  const [projectState, setProjectState] = useState<ProjectState>({ status: 'idle' })
  const [logoutError, setLogoutError] = useState<string | null>(null)
  const [preferenceError, setPreferenceError] = useState<string | null>(null)
  // 業務模块 filter。任务中心と工作空间の双方が読む。sidebar の子菜单が唯一の切替入口。
  // 空は「模块を持たない Project」= 絞り込み無しで、選べる module がある限り空にはしない。
  const [activeModuleId, setActiveModuleId] = useState('')
  const [modules, setModules] = useState<ProjectModuleRecord[]>([])
  // 表示言語。初期値は browser 言語で、認証後に保存済み preference が上書きする。
  const [language, setLanguage] = useState<UiLanguage>(
    () => resolveUiLanguage(null, navigator.languages ?? []),
  )
  const [languageError, setLanguageError] = useState<string | null>(null)
  const messages = MESSAGES[language]

  // 業務模块は sidebar の子菜单と各画面の絞り込みが共有するため、shell が唯一の取得元になる。
  // Project を跨いだ module 選択は無効なので、切替時にいったん捨ててから読み直す。
  useEffect(() => {
    setModules([])
    setActiveModuleId('')
    if (!projectId) return
    const controller = new AbortController()
    void loadProjectModules(projectId, controller.signal)
      .then((loaded) => {
        if (controller.signal.aborted) return
        setModules(loaded)
        // 「全部」入口を持たない子菜单なので、既定は先頭 module。module を持たない Project
        // だけが空のまま残り、そこでは絞り込み自体が働かず公開済み task が全部見える。
        setActiveModuleId(loaded[0]?.module_id ?? '')
      })
      .catch(() => {
        // 取得失敗時は絞り込み無しで続ける。主導航と実行導線を module 取得の失敗で止めない。
        if (!controller.signal.aborted) setModules([])
      })
    return () => controller.abort()
  }, [projectId])

  useEffect(() => {
    const controller = new AbortController()
    void loadAuthSession(controller.signal)
      .then((session) => setAuthState(session
        ? { status: 'authenticated', session }
        : { status: 'anonymous' }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setAuthState({
            status: 'anonymous',
            message: error instanceof Error ? error.message : messages.app.cannotVerifySession,
          })
        }
      })
    return () => controller.abort()
  }, [])

  // 認証後に保存済み言語 preference を読み、browser 既定より優先して適用する。
  useEffect(() => {
    if (authState.status !== 'authenticated') return
    const controller = new AbortController()
    void loadUiLanguage(controller.signal)
      .then((saved) => {
        if (saved !== null) setLanguage(resolveUiLanguage(saved, navigator.languages ?? []))
      })
      .catch(() => {
        // 読み込み失敗時は browser 既定のまま表示を続け、切替操作時の保存 error だけを表示する。
      })
    return () => controller.abort()
  }, [authState.status === 'authenticated' ? authState.session.user.user_id : null])

  // Browser tab だけで現在画面を判別できるよう、route と言語に合わせて文書 title を同期する。
  useEffect(() => {
    const entry = APP_ROUTES.find((candidate) => candidate.route === route)
    document.title = entry
      ? `${messages.routes[entry.route].label} · ProjectMind`
      : 'ProjectMind'
  }, [route, messages])

  // 読み上げ環境が現在言語で発音できるよう、<html lang> を表示言語へ同期する。
  useEffect(() => {
    document.documentElement.lang = { zh: 'zh-CN', ja: 'ja', en: 'en' }[language]
  }, [language])

  useEffect(() => {
    const controller = new AbortController()
    void loadMeta(controller.signal)
      .then((meta) => setMetaState({ status: 'ready', meta }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setMetaState({
            status: 'error',
            message: error instanceof Error ? error.message : 'Unknown API error',
          })
        }
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    /** Browser の戻る・進む操作も画面と認可済み Project context へ同期する。 */
    const handleHashChange = (): void => {
      const nextRoute = routeFromHash(window.location.hash)
      const context = routeContextFromHash(window.location.hash)
      setRoute(nextRoute)
      // 一覧が未着の間は選択に触れない。読み込み完了時の解決が正しい選択を入れる。
      if (projectState.status !== 'ready') {
        if (projectId) window.history.replaceState(null, '', routeHref(nextRoute, projectId, context))
        return
      }
      const selected = resolveProjectSelection(
        projectState.projects,
        projectIdFromHash(window.location.hash),
        projectId,
      )
      setProjectId(selected)
      window.history.replaceState(null, '', routeHref(nextRoute, selected, context))
    }
    window.addEventListener('hashchange', handleHashChange)
    return () => window.removeEventListener('hashchange', handleHashChange)
  }, [projectId, projectState])

  useEffect(() => {
    if (authState.status !== 'authenticated') {
      setProjectState({ status: 'idle' })
      setProjectId('')
      return
    }
    const controller = new AbortController()
    setProjectState({ status: 'loading' })
    void Promise.all([
      loadProjects(false, controller.signal),
      loadProjectPreference(controller.signal),
    ])
      .then(([projects, preference]) => {
        setProjectState({ status: 'ready', projects })
        const selected = resolveProjectSelection(
          projects,
          projectIdFromHash(window.location.hash),
          preference.project_id,
        )
        setProjectId(selected)
        window.history.replaceState(
          null,
          '',
          routeHref(
            routeFromHash(window.location.hash),
            selected,
            routeContextFromHash(window.location.hash),
          ),
        )
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        if (error instanceof ApiProblemError && error.status === 401) {
          setAuthState({ status: 'anonymous', message: messages.app.sessionExpired })
          return
        }
        setProjectState({
          status: 'error',
          message: error instanceof Error ? error.message : messages.app.loadProjectsFailed,
        })
      })
    return () => controller.abort()
  }, [authState.status === 'authenticated' ? authState.session.user.user_id : null])

  useEffect(() => {
    if (authState.status !== 'authenticated' || projectState.status !== 'ready') return
    const selectedProjectId = projectState.projects.some(
      ({ project_id }) => project_id === projectId,
    ) ? projectId : ''
    window.history.replaceState(
      null,
      '',
      routeHref(
        routeFromHash(window.location.hash),
        selectedProjectId,
        routeContextFromHash(window.location.hash),
      ),
    )
    if (projectId && !selectedProjectId) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      setPreferenceError(null)
      void saveProjectPreference(
        selectedProjectId || null,
        authState.session.csrf_token,
        controller.signal,
      ).catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setPreferenceError(
            error instanceof Error ? error.message : messages.app.savePreferenceFailed,
          )
        }
      })
    }, 150)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [authState, projectId, projectState])

  if (authState.status === 'loading') {
    return (
      <LanguageProvider language={language}>
        <main className="authShell">
          <div className="authLoading">{messages.app.verifyingSession}</div>
        </main>
      </LanguageProvider>
    )
  }
  if (authState.status === 'anonymous') {
    return (
      <LanguageProvider language={language}>
        <LoginPage
          initialError={authState.message}
          onAuthenticated={(session) => setAuthState({ status: 'authenticated', session })}
        />
      </LanguageProvider>
    )
  }

  const replaceProject = (project: ProjectRecord): void => {
    if (projectState.status !== 'ready') return
    setProjectState({
      status: 'ready',
      projects: [
        ...projectState.projects.filter(({ project_id }) => project_id !== project.project_id),
        project,
      ].sort((left, right) => left.name.localeCompare(right.name)),
    })
  }
  const removeProject = (archived: ProjectRecord): void => {
    if (projectState.status !== 'ready') return
    const projects = projectState.projects.filter(
      ({ project_id }) => project_id !== archived.project_id,
    )
    setProjectState({ status: 'ready', projects })
    if (projectId === archived.project_id) setProjectId(projects[0]?.project_id ?? '')
  }
  const performLogout = async (): Promise<void> => {
    setLogoutError(null)
    try {
      await logout(authState.session.csrf_token)
      setAuthState({ status: 'anonymous' })
    } catch (error) {
      setLogoutError(error instanceof Error ? error.message : messages.app.logoutFailed)
    }
  }
  const selectLanguage = (next: UiLanguage): void => {
    // 表示は即時に切替え、保存失敗は sidebar の共有 error 欄で通知する。
    setLanguage(next)
    setLanguageError(null)
    void saveUiLanguage(next, authState.session.csrf_token).catch((error: unknown) => {
      setLanguageError(
        error instanceof Error ? error.message : MESSAGES[next].app.saveLanguageFailed,
      )
    })
  }
  // 画面には UUID ではなく Project 名で現在 context を示す。未選択・未読込は null で表す。
  const currentProject = projectState.status === 'ready'
    ? projectState.projects.find(({ project_id }) => project_id === projectId) ?? null
    : null
  const routeContext = routeContextFromHash(window.location.hash)

  return (
    <LanguageProvider language={language}>
      <div className="appFrame">
        <AppNavigation
          currentRoute={route}
          logoutError={logoutError ?? preferenceError ?? languageError}
          metaState={metaState}
          onLogout={() => void performLogout()}
          onSelectLanguage={selectLanguage}
          onSelectProject={setProjectId}
          onSelectModule={(moduleId) => {
            setActiveModuleId(moduleId)
            // 絞り込みが効く画面に居るなら、その場で範囲だけを切り替える(見ている画面を
            // 奪わない)。効かない画面から選んだときだけ、模块の内容を見せられる主画面へ移す。
            if (routeUsesModuleFilter(route)) return
            window.location.hash = routeHref('workspace', projectId || undefined)
          }}
          projectId={projectId}
          projectState={projectState}
          user={authState.session.user}
          activeModuleId={activeModuleId}
          modules={modules}
        />
        <main className="shell">
          {renderPage(
            route,
            projectId,
            currentProject,
            setProjectId,
            metaState,
            authState.session,
            projectState,
            replaceProject,
            removeProject,
            activeModuleId,
            routeContext.runId,
            routeContext.taskId,
          )}
        </main>
      </div>
    </LanguageProvider>
  )
}

/** 選択された route を対応する画面 component へ変換する。 */
function renderPage(
  route: AppRoute,
  projectId: string,
  currentProject: ProjectRecord | null,
  setProjectId: (projectId: string) => void,
  metaState: MetaState,
  session: AuthSessionRecord,
  projectState: ProjectState,
  onProjectChanged: (project: ProjectRecord) => void,
  onProjectArchived: (project: ProjectRecord) => void,
  activeModuleId: string,
  initialRunId: string | null,
  initialTaskId: string | null,
): ReactNode {
  switch (route) {
    case 'skills':
      return <SkillsPage csrfToken={session.csrf_token} projectId={projectId} />
    case 'projects':
      return <ProjectsPage
        onProjectArchived={onProjectArchived}
        onProjectChanged={onProjectChanged}
        projectId={projectId}
        projectState={projectState}
        session={session}
        setProjectId={setProjectId}
      />
    case 'documents':
      return <DocumentsPage csrfToken={session.csrf_token} projectId={projectId} />
    case 'resources':
      return <ResourcesPage csrfToken={session.csrf_token} projectId={projectId} />
    case 'tasks':
      return <TasksPage key={`${session.user.user_id}:${projectId}`} csrfToken={session.csrf_token} moduleId={activeModuleId} projectId={projectId} />
    case 'workspace':
      return <WorkspacePage
        key={`${session.user.user_id}:${projectId}`}
        actorId={session.user.user_id}
        csrfToken={session.csrf_token}
        initialRunId={initialRunId}
        initialTaskId={initialTaskId}
        moduleId={activeModuleId}
        projectId={projectId}
      />
    case 'history':
      return <HistoryPage projectId={projectId} />
    case 'home':
      return <HomePage metaState={metaState} project={currentProject} projectId={projectId} />
  }
}

/** Application shell の認証 gate state。 */
type AuthState =
  | { status: 'loading' }
  | { status: 'anonymous'; message?: string }
  | { status: 'authenticated'; session: AuthSessionRecord }
