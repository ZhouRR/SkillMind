import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { ApiProblemError, loadProject, loadProjectPreference, loadProjects, type AuthSessionRecord, type ProjectRecord } from '../api'
import type { ProjectState } from '../appState'
import { projectRequestFromHash, resolveProjectSelection } from '../lib/projectContext'
import { routeFromHash } from '../lib/routing'

const PROJECT_READ_TIMEOUT_MS = 30_000

/** 対象の認可が未確認の間は、以前の Project を業務画面へ渡さない。 */
export type ProjectAccess =
  | { status: 'loading' | 'empty' | 'unavailable' | 'error' }
  | { status: 'ready'; project: ProjectRecord }

/** 一覧と詳細は別々に確認し、一方の失敗で他方の認可結果を代用しない。 */
type ReadState<T> = { request: object } & (
  | { status: 'ready'; data: T }
  | { status: 'error' | 'unavailable' }
)

/** Project 読取の対象・会話・期限を守る。再読取開始時も旧認可を失効させる。 */
function useProjectRead<T>(key: string, loader: ((signal: AbortSignal) => Promise<T>) | null, currentSession: () => boolean, onSessionEnded: () => void) {
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<ReadState<T> | null>(null)
  const active = useRef<AbortController | null>(null)
  // A → B → A でも新しい読取 identity にし、前回 A の ready を再利用しない。
  const request = useMemo(() => ({ key, loader, revision }), [key, loader, revision])
  useLayoutEffect(() => {
    if (!loader) return
    const controller = new AbortController()
    active.current = controller
    const deadline = performance.now() + PROJECT_READ_TIMEOUT_MS
    /** Abort を無視した response と、切替済み会話からの 401 を破棄する。 */
    const current = (): boolean => active.current === controller && !controller.signal.aborted && currentSession()
    const expire = (): void => {
      if (!current()) return
      controller.abort()
      setState({ request, status: 'error' })
    }
    const timer = window.setTimeout(expire, PROJECT_READ_TIMEOUT_MS)
    void loader(controller.signal).then((data) => {
      if (!current()) return
      if (performance.now() >= deadline) { expire(); return }
      setState({ request, status: 'ready', data })
    }).catch((error: unknown) => {
      if (!current()) return
      if (performance.now() >= deadline) { expire(); return }
      if (error instanceof ApiProblemError && error.status === 401) onSessionEnded()
      setState({ request, status: error instanceof ApiProblemError && [403, 404].includes(error.status) ? 'unavailable' : 'error' })
    }).finally(() => window.clearTimeout(timer))
    return () => {
      controller.abort()
      window.clearTimeout(timer)
      if (active.current === controller) active.current = null
    }
  }, [request])
  /** 操作と同 tick に古い promise が解決しても、再読取結果として採用しない。 */
  const refresh = useCallback(() => {
    active.current?.abort()
    setRevision((value) => value + 1)
  }, [])
  return { state: state?.request === request ? state : null, refresh }
}

/** URL の意図、活動一覧と認可済み詳細を分離する shell 専用 context。 */
export function useProjectContext(session: AuthSessionRecord | null, hash: string, isCurrentSession: (session: AuthSessionRecord) => boolean, onSessionEnded: () => void, listError: string) {
  const owner = session ? `${session.user.user_id}:${session.csrf_token}` : ''
  const [remembered, setRemembered] = useState({ owner: '', projectId: '' })
  const request = projectRequestFromHash(hash)
  const loadList = useCallback((signal: AbortSignal) => loadProjects(false, signal), [])
  const list = useProjectRead(owner, session ? loadList : null, () => session !== null && isCurrentSession(session), onSessionEnded)
  const preference = useProjectRead(owner, session ? loadProjectPreference : null, () => session !== null && isCurrentSession(session), onSessionEnded)
  const projects = list.state?.status === 'ready' ? list.state.data : []
  const candidate = request.kind === 'invalid' ? '' : request.kind === 'explicit' ? request.projectId
    : remembered.owner === owner && remembered.projectId ? remembered.projectId
      : preference.state ? resolveProjectSelection(projects, null, preference.state.status === 'ready' ? preference.state.data.project_id : null) : ''
  const target = candidate.toLowerCase()
  const detailLoader = useCallback((signal: AbortSignal) => loadProject(target, signal), [target])
  const detail = useProjectRead(`${owner}:${target}:${routeFromHash(hash)}`, session && target ? detailLoader : null,
    () => {
      if (!session || !isCurrentSession(session)) return false
      // hashchange の配信前でも、別の明示対象へ移った response は受け取らない。
      const live = projectRequestFromHash(window.location.hash)
      return live.kind === 'absent' || (live.kind === 'explicit' && live.projectId.toLowerCase() === target)
    }, onSessionEnded)
  useLayoutEffect(() => {
    // 一度選んだ対象は preference ではない。資格失効・一覧更新でも別 Project に換えない。
    if (session && target) setRemembered({ owner, projectId: target })
  }, [owner, target])

  let access: ProjectAccess
  if (!session) access = { status: 'loading' }
  else if (request.kind === 'invalid') access = { status: 'unavailable' }
  else if (!target) access = { status: !list.state || !preference.state ? 'loading' : list.state.status === 'ready' ? 'empty' : 'error' }
  else if (!detail.state) access = { status: 'loading' }
  else access = detail.state.status === 'ready' ? { status: 'ready', project: detail.state.data } : { status: detail.state.status }

  const projectState: ProjectState = list.state?.status === 'ready'
    ? { status: 'ready', projects }
    : list.state ? { status: 'error', message: listError } : { status: 'loading' }
  const projectId = access.status === 'ready' ? access.project.project_id : ''
  // 不正な複数値の先頭が活動 ID でも、選択欄に認可済みとは表示しない。
  const selectionId = projectId || (request.kind === 'invalid' ? `unavailable:${request.value}` : candidate)
  const selectionState: ProjectState = projectState.status === 'ready' && access.status !== 'ready'
    ? { status: 'ready', projects: projects.filter((project) => project.project_id.toLowerCase() !== target) }
    : projectState

  /** 再読取は同じ明示 URL を保持し、一覧と認可の両方を更新する。 */
  const refresh = (): void => { list.refresh(); preference.refresh(); detail.refresh() }
  /** 明示選択だけが Account からも共有選択を変更できる。詳細は選択後に確認する。 */
  const choose = (projectId: string): void => { setRemembered({ owner, projectId }) }
  const preferenceFailed = preference.state !== null && preference.state.status !== 'ready'
  return { access, projectId, selectionId, selectionState, projectState, refresh, choose, preferenceFailed }
}
