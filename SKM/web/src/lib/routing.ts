/** Application shell が表示できる固定画面。 */
export type AppRoute = 'home' | 'skills' | 'projects' | 'accounts' | 'documents' | 'resources' | 'tasks' | 'schedules' | 'workspace' | 'history'

/** 画面の所属。platform は Project 非依存、project は現在 Project の作業区。 */
export type RouteScope = 'platform' | 'project'

/** Navigation が共有する route 定義。配列順がそのまま sidebar の並び順になる。

    表示名と概要は言語別 catalog(`lib/i18n/messages.ts` の `routes`)が持ち、
    ここは画面非依存の構造(識別子と所属)だけを固定する。 */
export const APP_ROUTES: ReadonlyArray<{
  route: AppRoute
  scope: RouteScope
}> = [
  { route: 'home', scope: 'platform' },
  // 任务中心が「何を走らせるか」、工作空间が「今走っている一つを観る」。この二分が導航の要。
  // 項目内の主画面は工作空间とし、先頭に置く:模块を選んだ直後に見たいのは実行台と履歴で、
  // 業務模块の子菜单もこの主画面へ入れ子にする。
  { route: 'workspace', scope: 'project' },
  { route: 'history', scope: 'project' },
  { route: 'tasks', scope: 'project' },
  { route: 'schedules', scope: 'project' },
  { route: 'documents', scope: 'project' },
  { route: 'resources', scope: 'project' },
  // Skills 解析は資産を作る平台能力として platform 組に置く。保存先は sidebar の現在 Project。
  { route: 'skills', scope: 'platform' },
  { route: 'projects', scope: 'platform' },
  { route: 'accounts', scope: 'platform' },
]

/** 業務模块の絞り込みが効く画面かどうかを返す。

    子菜单の強調表示はこの判定だけを見る。任务中心と工作空间は同じ module 選択を読むため、
    片方の画面でしか強調しないと「選んだのに反映されていない」ように見える。 */
export function routeUsesModuleFilter(route: AppRoute): boolean {
  return route === 'tasks' || route === 'workspace'
}

/** URL hash を既知の画面へ正規化し、不明な値は主页へ安全に戻す。 */
export function routeFromHash(hash: string): AppRoute {
  const candidate = hash.replace(/^#\/?/, '').split('?', 1)[0]?.replace(/\/+$/, '') ?? ''
  return APP_ROUTES.some(({ route }) => route === candidate)
    ? candidate as AppRoute
    : 'home'
}

/** URL に保持する画面コンテキスト。Run/Task の ID は API から返った値だけを使う。 */
export interface RouteContext {
  runId?: string | null
  taskId?: string | null
}

/** 固定画面を static hosting と互換な hash URL へ変換する。 */
export function routeHref(route: AppRoute, projectId?: string, context?: RouteContext): string {
  const base = route === 'home' ? '#/' : `#/${route}`
  // Account の検索や対象は画面内に限定し、Project/Run の URL 文脈を引き継がない。
  if (route === 'accounts') return base
  const query = new URLSearchParams()
  if (projectId) query.set('project', projectId)
  if (context?.runId) query.set('run', context.runId)
  if (context?.taskId) query.set('task', context.taskId)
  const suffix = query.toString()
  return suffix ? `${base}?${suffix}` : base
}

/** 画面間の導航は不正・重複を含む明示 Project を保持し、旧 Run/Task は引き継がない。 */
export function routeHrefWithProject(route: AppRoute, hash: string, fallbackProjectId?: string): string {
  if (route === 'accounts') return routeHref(route)
  const request = new URLSearchParams(hash.includes('?') ? hash.slice(hash.indexOf('?') + 1) : '')
  if (routeFromHash(hash) === 'accounts' || !request.has('project')) return routeHref(route, fallbackProjectId)
  const query = new URLSearchParams()
  for (const value of request.getAll('project')) query.append('project', value)
  return `${routeHref(route)}?${query.toString()}`
}

/** URL hash から明示された Project context を取得する。 */
export function projectIdFromHash(hash: string): string | null {
  return routeContextFromHash(hash).projectId
}

/** URL hash から Project と、画面を開く対象の Run/Task を取得する。 */
export function routeContextFromHash(hash: string): { projectId: string | null; runId: string | null; taskId: string | null } {
  if (routeFromHash(hash) === 'accounts') return { projectId: null, runId: null, taskId: null }
  const query = hash.split('?', 2)[1]
  if (query === undefined) return { projectId: null, runId: null, taskId: null }
  const params = new URLSearchParams(query)
  return {
    projectId: params.get('project'),
    runId: params.get('run'),
    taskId: params.get('task'),
  }
}
