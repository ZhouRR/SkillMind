import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import {
  ApiProblemError,
  loadAuthSession,
  loadMeta,
  loadProjectModules,
  loadUiLanguage,
  logout,
  saveProjectPreference,
  saveUiLanguage,
  type AuthSessionRecord,
  type ProjectModuleRecord,
  type ProjectRecord,
  type UserAccountRecord,
} from './api'
import { type MetaState, type ProjectState } from './appState'
import { AppNavigation } from './components/AppNavigation'
import { ProjectContextNotice } from './components/ProjectContextNotice'
import { LanguageProvider } from './i18n'
import { navigateHash, useHashRoute } from './hooks/useHashRoute'
import { useProjectContext } from './hooks/useProjectContext'
import type { SessionEnded } from './hooks/useUserRequest'
import { MESSAGES, type UiLanguage } from './lib/i18n/messages'
import { resolveUiLanguage } from './lib/i18n/resolve'
import { nextProjectManagementBoundary, projectRequestFromHash } from './lib/projectContext'
import { sameUser } from './lib/userFeedback'
import { AccountsPage } from './pages/AccountsPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { HomePage } from './pages/HomePage'
import { HistoryPage } from './pages/HistoryPage'
import { LoginPage } from './pages/LoginPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { ResourcesPage } from './pages/ResourcesPage'
import { SchedulesPage } from './pages/SchedulesPage'
import { SkillsPage } from './pages/SkillsPage'
import { TasksPage } from './pages/TasksPage'
import { WorkspacePage } from './pages/WorkspacePage'
import {
  APP_ROUTES,
  routeContextFromHash,
  routeFromHash,
  routeHref,
  routeUsesModuleFilter,
  type AppRoute,
} from './lib/routing'
import './styles.css'

/** Skillmind の画面選択と共有 Project context を管理する application shell。 */
export function App() {
  const hash = useHashRoute()
  const route = routeFromHash(hash)
  const [metaState, setMetaState] = useState<MetaState>({ status: 'loading' })
  const [authState, setAuthState] = useState<AuthState>({ status: 'loading' })
  const [logoutError, setLogoutError] = useState<string | null>(null)
  const [preferenceError, setPreferenceError] = useState<string | null>(null)
  // 業務模块 filter。任务中心と工作空间の双方が読む。sidebar の子菜单が唯一の切替入口。
  // 空は「模块を持たない Project」= 絞り込み無しで、選べる module がある限り空にはしない。
  const [activeModuleId, setActiveModuleId] = useState('')
  const [modules, setModules] = useState<ProjectModuleRecord[]>([])
  const [moduleContext, setModuleContext] = useState('')
  const moduleChoice = useRef<{ context: string; moduleId: string } | null>(null)
  // 表示言語。初期値は browser 言語で、認証後に保存済み preference が上書きする。
  const [language, setLanguage] = useState<UiLanguage>(
    () => resolveUiLanguage(null, navigator.languages ?? []),
  )
  const [languageError, setLanguageError] = useState<string | null>(null)
  const [logoutPending, setLogoutPending] = useState(false)
  const [accountContextRevision, setAccountContextRevision] = useState(0)
  const mounted = useRef(true)
  const authentication = useRef<AuthState>(authState)
  const logoutRequest = useRef<AbortController | null>(null)
  const languageRequest = useRef<AbortController | null>(null)
  const languageChoice = useRef(0)
  const messages = MESSAGES[language]
  const sessionKey = authState.status === 'authenticated'
    ? `${authState.session.user.user_id}:${authState.session.csrf_token}` : ''

  /** 旧会話から遅れて到着した response が、次のログインを書き換えないための境界。 */
  const isCurrentSession = useCallback((session: AuthSessionRecord): boolean => {
    const current = authentication.current
    return mounted.current && current.status === 'authenticated'
      && sameUser(current.session.user.user_id, session.user.user_id)
      && current.session.csrf_token === session.csrf_token
  }, [])

  /** React の描画を待たずに認証世代を替え、別会話の Project と非同期書込を破棄する。 */
  const replaceAuthentication = useCallback((next: AuthState): void => {
    const sameSession = next.status === 'authenticated' && isCurrentSession(next.session)
    authentication.current = next
    setAuthState(next)
    if (sameSession) return
    logoutRequest.current?.abort()
    languageRequest.current?.abort()
    logoutRequest.current = null
    languageRequest.current = null
    setLogoutPending(false)
    setLogoutError(null)
    setPreferenceError(null)
    setLanguageError(null)
    setModules([])
    setModuleContext('')
    moduleChoice.current = null
    setActiveModuleId('')
  }, [isCurrentSession])

  /** 自己失効の受理後に logout を重ねず、対象の会話だけをログイン画面へ戻す。 */
  const endSession = (session: AuthSessionRecord, reason: 'expired' | 'revoked' = 'expired'): void => {
    if (!isCurrentSession(session)) return
    replaceAuthentication({
      status: 'anonymous',
      message: reason === 'revoked' ? messages.account.sessionEnded : messages.app.sessionExpired,
    })
  }

  const projectContext = useProjectContext(
    authState.status === 'authenticated' ? authState.session : null,
    hash, isCurrentSession,
    () => { if (authState.status === 'authenticated') endSession(authState.session) },
    messages.app.loadProjectsFailed,
  )
  const { projectId, projectState, access } = projectContext
  const [managementBoundary, setManagementBoundary] = useState({ owner: '', selectionId: '', revision: 0 })
  const nextManagementBoundary = nextProjectManagementBoundary(
    managementBoundary, sessionKey, projectContext.selectionId, projectRequestFromHash(hash),
  )
  // Effect 後に破棄するのではなく、対象変更と同じ render で旧管理 subtree を閉じる。
  if (nextManagementBoundary !== managementBoundary) setManagementBoundary(nextManagementBoundary)
  const managementContextKey = `projects:${nextManagementBoundary.revision}`

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      logoutRequest.current?.abort()
      languageRequest.current?.abort()
    }
  }, [])

  // 業務模块は sidebar の子菜单と各画面の絞り込みが共有するため、shell が唯一の取得元になる。
  // Project を跨いだ module 選択は無効なので、切替時にいったん捨ててから読み直す。
  useEffect(() => {
    setModules([])
    setActiveModuleId('')
    if (!projectId || authState.status !== 'authenticated') return
    const session = authState.session
    const controller = new AbortController()
    void loadProjectModules(projectId, controller.signal)
      .then((loaded) => {
        if (controller.signal.aborted || !isCurrentSession(session)) return
        setModules(loaded)
        setModuleContext(`${sessionKey}:${projectId}`)
        // 同じ Project の再認可で、人が選んだ module を先頭へ戻さない。
        // 前 Project の ID や現在一覧から消えた module は新しい候補へ持ち越さない。
        const context = `${sessionKey}:${projectId}`
        const chosen = moduleChoice.current
        const selected = chosen?.context === context && loaded.some((item) => item.module_id === chosen.moduleId)
          ? chosen.moduleId : loaded[0]?.module_id ?? ''
        moduleChoice.current = { context, moduleId: selected }
        setActiveModuleId(selected)
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || !isCurrentSession(session)) return
        if (error instanceof ApiProblemError && error.status === 401) {
          endSession(session)
          return
        }
        // 取得失敗時は絞り込み無しで続ける。主導航と実行導線を module 取得の失敗で止めない。
        setModules([])
      })
    return () => controller.abort()
  }, [projectId, sessionKey])

  useEffect(() => {
    const controller = new AbortController()
    void loadAuthSession(controller.signal)
      .then((session) => {
        if (!controller.signal.aborted) replaceAuthentication(session
          ? { status: 'authenticated', session }
          : { status: 'anonymous' })
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          replaceAuthentication({
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
    const session = authState.session
    const choice = languageChoice.current
    const controller = new AbortController()
    void loadUiLanguage(controller.signal)
      .then((saved) => {
        if (!controller.signal.aborted && isCurrentSession(session)
          && choice === languageChoice.current && saved !== null) {
          setLanguage(resolveUiLanguage(saved, navigator.languages ?? []))
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || !isCurrentSession(session)) return
        if (error instanceof ApiProblemError && error.status === 401) endSession(session)
        // 読み込み失敗時は browser 既定のまま表示を続け、切替操作時の保存 error だけを表示する。
      })
    return () => controller.abort()
  }, [sessionKey])

  // Browser tab だけで現在画面を判別できるよう、route と言語に合わせて文書 title を同期する。
  useEffect(() => {
    const entry = APP_ROUTES.find((candidate) => candidate.route === route)
    document.title = entry
      ? `${messages.routes[entry.route].label} · Skillmind`
      : 'Skillmind'
  }, [route, messages])

  // 読み上げ環境が現在言語で発音できるよう、<html lang> を表示言語へ同期する。
  useEffect(() => {
    document.documentElement.lang = { zh: 'zh-CN', ja: 'ja', en: 'en' }[language]
  }, [language])

  useEffect(() => {
    const controller = new AbortController()
    void loadMeta(controller.signal)
      .then((meta) => {
        if (!controller.signal.aborted) setMetaState({ status: 'ready', meta })
      })
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
    if (authState.status !== 'authenticated' || access.status !== 'ready' || access.project.status !== 'ACTIVE') return
    const session = authState.session
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      setPreferenceError(null)
      void saveProjectPreference(
        projectId,
        session.csrf_token,
        controller.signal,
      ).catch((error: unknown) => {
        if (!controller.signal.aborted && isCurrentSession(session)) {
          if (error instanceof ApiProblemError && error.status === 401) {
            endSession(session)
            return
          }
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
  }, [sessionKey, projectId, access.status === 'ready' ? access.project.status : access.status])

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
          onAuthenticated={(session) => replaceAuthentication({ status: 'authenticated', session })}
        />
      </LanguageProvider>
    )
  }

  const replaceProject = (_project: ProjectRecord): void => {
    if (!isCurrentSession(authState.session)) return
    // 編集・復元の response だけで継続認可とは扱わず、現在対象を再読取する。
    projectContext.refresh()
  }
  /** 初回一覧の到着は草稿を壊さず、人が Project を替えた時だけ Account 文脈を捨てる。 */
  const selectProject = (next: string): void => {
    if (!isCurrentSession(authState.session)) return
    if (next === projectId) return
    setAccountContextRevision((revision) => revision + 1)
    projectContext.choose(next)
    // 意図的な Project 変更だけが URL を変更し、旧 Run/Task を持ち越さない。
    navigateHash(routeHref(route, next))
  }
  const performLogout = async (): Promise<void> => {
    if (logoutRequest.current || !isCurrentSession(authState.session)) return
    const session = authState.session
    const controller = new AbortController()
    logoutRequest.current = controller
    setLogoutPending(true)
    setLogoutError(null)
    try {
      await logout(session.csrf_token, controller.signal)
      if (!controller.signal.aborted && isCurrentSession(session)) {
        replaceAuthentication({ status: 'anonymous' })
      }
    } catch (error) {
      if (!controller.signal.aborted && isCurrentSession(session)) {
        if (error instanceof ApiProblemError && error.status === 401) endSession(session)
        else setLogoutError(messages.app.logoutFailed)
      }
    } finally {
      if (logoutRequest.current === controller) {
        logoutRequest.current = null
        setLogoutPending(false)
      }
    }
  }
  const selectLanguage = (next: UiLanguage): void => {
    if (!isCurrentSession(authState.session)) return
    const session = authState.session
    languageChoice.current += 1
    languageRequest.current?.abort()
    const controller = new AbortController()
    languageRequest.current = controller
    // 表示は即時に切替え、保存失敗は sidebar の共有 error 欄で通知する。
    setLanguage(next)
    setLanguageError(null)
    void saveUiLanguage(next, session.csrf_token, controller.signal).catch((error: unknown) => {
      if (!controller.signal.aborted && isCurrentSession(session)) {
        if (error instanceof ApiProblemError && error.status === 401) endSession(session)
        else setLanguageError(MESSAGES[next].app.saveLanguageFailed)
      }
    }).finally(() => {
      if (languageRequest.current === controller) languageRequest.current = null
    })
  }
  /** 他人の変更は sidebar の本人表示へ混入させず、同じ会話の表示名だけを更新する。 */
  const accountChanged = (account: UserAccountRecord): void => {
    if (!isCurrentSession(authState.session) || !sameUser(account.user_id, authState.session.user.user_id)) return
    replaceAuthentication({
      status: 'authenticated',
      session: {
        ...authState.session,
        user: { ...authState.session.user, display_name: account.display_name },
      },
    })
  }
  // 画面には UUID ではなく Project 名で現在 context を示す。未選択・未読込は null で表す。
  const currentProject = access.status === 'ready' ? access.project : null
  const routeContext = routeContextFromHash(hash)
  const canRenderPage = APP_ROUTES.find((entry) => entry.route === route)?.scope === 'platform' || access.status === 'ready'
  const currentModules = moduleContext === `${sessionKey}:${projectId}` ? modules : []
  // 再読取中も同 Project の明示 filter を保ち、一瞬「全 module」の Task を表示しない。
  const rememberedModuleId = moduleChoice.current?.context === `${sessionKey}:${projectId}` ? moduleChoice.current.moduleId : ''
  const currentModuleId = currentModules.some((item) => item.module_id === activeModuleId) ? activeModuleId : rememberedModuleId

  return (
    <LanguageProvider language={language}>
      <div className="appFrame">
        <AppNavigation
          key={sessionKey}
          currentRoute={route}
          logoutError={logoutError ?? preferenceError ?? languageError ?? (projectContext.preferenceFailed ? messages.app.loadPreferenceFailed : null)}
          logoutPending={logoutPending}
          metaState={metaState}
          onLogout={() => void performLogout()}
          onSessionEnded={() => endSession(authState.session)}
          onSelectLanguage={selectLanguage}
          onSelectProject={selectProject}
          onRefreshProjects={projectContext.refresh}
          onSelectModule={(moduleId) => {
            if (!projectId) return
            moduleChoice.current = { context: `${sessionKey}:${projectId}`, moduleId }
            setActiveModuleId(moduleId)
            // 絞り込みが効く画面に居るなら、その場で範囲だけを切り替える(見ている画面を
            // 奪わない)。効かない画面から選んだときだけ、模块の内容を見せられる主画面へ移す。
            if (routeUsesModuleFilter(route)) return
            window.location.hash = routeHref('workspace', projectId || undefined)
          }}
          projectId={projectContext.selectionId}
          pendingProjectId={projectId}
          projectHash={hash}
          projectState={projectContext.selectionState}
          currentProject={currentProject}
          user={authState.session.user}
          activeModuleId={currentModuleId}
          modules={projectId ? currentModules : []}
        />
        <main data-page={route} className={route === 'accounts' ? 'shell accountsPage' : 'shell'}
          key={`${sessionKey}:${route === 'accounts' ? `accounts:${accountContextRevision}`
            : route === 'projects' ? managementContextKey : projectContext.selectionId.toLowerCase()}`}>
          {route !== 'accounts' && <ProjectContextNotice access={access} onRefresh={projectContext.refresh} />}
          {canRenderPage && renderPage(
            route,
            projectId,
            currentProject,
            selectProject,
            metaState,
            authState.session,
            projectState,
            replaceProject,
            replaceProject,
            currentModuleId,
            routeContext.runId,
            routeContext.taskId,
            (reason) => endSession(authState.session, reason),
            accountChanged,
            projectContext.selectionId,
            managementContextKey,
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
  onSessionEnded: SessionEnded,
  onAccountChanged: (account: UserAccountRecord) => void,
  projectContextId: string,
  managementContextKey: string,
): ReactNode {
  switch (route) {
    case 'accounts':
      return <AccountsPage session={session} onSessionEnded={onSessionEnded} onAccountChanged={onAccountChanged} />
    case 'skills':
      return <SkillsPage csrfToken={session.csrf_token} projectId={projectId} />
    case 'projects':
      return <ProjectsPage
        currentProject={currentProject}
        projectContextId={projectContextId}
        managementContextKey={managementContextKey}
        onSessionEnded={onSessionEnded}
        onProjectArchived={onProjectArchived}
        onProjectDeleted={onProjectArchived}
        onProjectChanged={onProjectChanged}
        projectId={projectId}
        projectState={projectState}
        session={session}
        setProjectId={setProjectId}
      />
    case 'documents':
      return <DocumentsPage csrfToken={session.csrf_token} projectId={projectId}
        actorId={session.user.user_id} readOnly={currentProject?.status !== 'ACTIVE'} onSessionEnded={onSessionEnded} />
    case 'resources':
      return <ResourcesPage csrfToken={session.csrf_token} projectId={projectId} />
    case 'tasks':
      return <TasksPage key={`${session.user.user_id}:${projectId}`} csrfToken={session.csrf_token} actorId={session.user.user_id} onSessionEnded={onSessionEnded} moduleId={activeModuleId} projectId={projectId}
        projectReadOnly={currentProject?.status !== 'ACTIVE'} />
    case 'schedules':
      return <SchedulesPage projectId={projectId} actorId={session.user.user_id} csrfToken={session.csrf_token}
        currentProject={currentProject} onSessionEnded={onSessionEnded} />
    case 'workspace':
      return <WorkspacePage
        key={`${session.user.user_id}:${projectId}`}
        actorId={session.user.user_id}
        projectReadOnly={currentProject?.status !== 'ACTIVE'}
        csrfToken={session.csrf_token}
        initialRunId={initialRunId}
        initialTaskId={initialTaskId}
        moduleId={activeModuleId}
        onSessionExpired={onSessionEnded}
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
