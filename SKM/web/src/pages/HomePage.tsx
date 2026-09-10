import { useEffect, useState } from 'react'

import type { MetaState } from '../appState'
import { loadRunHistory, type ProjectRecord, type RunHistoryItemRecord } from '../api'
import { EmptyState, LoadingSkeleton, PageHeader, StatusBadge } from '../components/PageElements'
import { PendingActionsPanel } from '../components/PendingActionsPanel'
import { ROUTE_ICONS } from '../components/routeIcons'
import { useMessages } from '../i18n'
import { routeHref } from '../lib/routing'
import { formatLocalTimestamp, runHistoryTitle } from '../lib/presentation'

const RECENT_RUNS_LIMIT = 6

/** 概览の「最近执行」読み込み state。 */
type RecentRunsState =
  | { status: 'idle' | 'loading' }
  | { status: 'ready'; items: RunHistoryItemRecord[] }
  | { status: 'error'; message: string }

/** 現在 Project の「今どうなっているか」を一枚で示す概览画面。
 *
 * 以前はここに全画面への card 一覧を並べていたが、それは sidebar の導航をそのまま複製した
 * ものだった。概览が答えるべきなのは「入口はどこか」ではなく「**私が今なにをすべきか**」——
 * 待機中の Run、直近の実行、接続状態の三つ。
 */
export function HomePage({ metaState, project, projectId }: {
  metaState: MetaState
  project: ProjectRecord | null
  projectId: string
}) {
  const messages = useMessages()
  const [runsState, setRunsState] = useState<RecentRunsState>({ status: 'idle' })

  useEffect(() => {
    if (!projectId) {
      setRunsState({ status: 'idle' })
      return
    }
    const controller = new AbortController()
    setRunsState({ status: 'loading' })
    void loadRunHistory(projectId, RECENT_RUNS_LIMIT, 0, controller.signal)
      .then((page) => setRunsState({ status: 'ready', items: page.items }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setRunsState({
            status: 'error',
            message: error instanceof Error ? error.message : messages.home.loadRunsFailed,
          })
        }
      })
    return () => controller.abort()
  }, [projectId])

  return (
    <div className="homePage">
      <PageHeader
        title={messages.routes.home.label}
        description={messages.home.description}
      />
      <section className="homeHero" aria-label={messages.home.currentProject}>
        <div className="homeHeroBody">
          <span className="homeEyebrow">{messages.home.currentProject}</span>
          <h2 title={project?.project_id}>{project ? project.name : messages.home.notSelected}</h2>
          <p>{project ? messages.home.startHint : messages.home.selectProjectHint}</p>
          {project && <span className="homeProjectKey">{ROUTE_ICONS.projects}<code>{project.key}</code></span>}
        </div>
        <div className="homeHeroAction">
          <span className="homeHeroGlyph" aria-hidden="true">{ROUTE_ICONS.tasks}</span>
          <a className="primaryButton" href={routeHref(project ? 'tasks' : 'projects', project ? projectId : undefined)}>
            {project ? messages.home.goTasks : messages.routes.projects.label}<span aria-hidden="true">↗</span>
          </a>
        </div>
      </section>
      {/* 対応待ちは DOM/視線の順とも先頭に残し、空状態は小さく保つ。 */}
      <div className="homeActivity">
        <aside className="homeAttention">
          <PendingActionsPanel projectId={projectId} />
          {metaState.status === 'error' && <ServiceStatCard state={metaState} />}
        </aside>
        <section className="panel homeRuns" aria-label={messages.home.recentRuns}>
          <div className="panelHeader">
            <h2>{messages.home.recentRuns}</h2>
            <div className="homeActions">
              <a href={routeHref('history', projectId || undefined)}>{messages.routes.history.label}</a>
            </div>
          </div>
          <RecentRuns projectId={projectId} state={runsState} />
        </section>
      </div>
    </div>
  )
}

/** 最近 Run の読み込み state を空態・error・一覧のいずれかへ描画する。 */
function RecentRuns({ projectId, state }: { projectId: string; state: RecentRunsState }) {
  const messages = useMessages()
  if (!projectId) return <EmptyState text={messages.home.emptyNoProject} />
  if (state.status === 'error') return <p className="error" role="alert">{state.message}</p>
  if (state.status !== 'ready') {
    return <LoadingSkeleton label={messages.home.readingHistory} rows={3} />
  }
  if (state.items.length === 0) {
    return (
      <EmptyState
        text={messages.home.emptyNoRuns}
        action={<a className="secondaryButton compactButton" href={routeHref('tasks', projectId)}>{messages.home.goTasks}</a>}
      />
    )
  }
  return (
    <ol className="homeRunList">
      {state.items.map((item) => (
        <li key={item.run_id}>
          <a className="homeRunItem" href={routeHref('workspace', projectId, { runId: item.run_id })}>
            <span className="homeRunTitle">
              <strong>{runHistoryTitle(item.result_summary, item.run_id, messages.elements.runFallbackTitle)}</strong>
              <small>{formatLocalTimestamp(item.created_at)}</small>
            </span>
            <StatusBadge status={item.status} />
          </a>
        </li>
      ))}
    </ol>
  )
}

/** API metadata の読み込み状態を概览用の状態 card として表示する。 */
function ServiceStatCard({ state }: { state: MetaState }) {
  const messages = useMessages()
  if (state.status === 'error') {
    return (
      <article className="statCard">
        <span className="statCardIcon">{ROUTE_ICONS.resources}<span className="statLabel">{messages.home.serviceStatus}</span></span>
        <strong className="statValue statError">{messages.home.connectFailed}</strong>
        <p className="statHint">{state.message}</p>
      </article>
    )
  }
  return (
    <article className="statCard">
      <span className="statCardIcon">{ROUTE_ICONS.resources}<span className="statLabel">{messages.home.serviceStatus}</span></span>
      <strong className={`statValue${state.status === 'ready' ? ' statReady' : ''}`}>
        {state.status === 'loading' ? messages.home.connectingShort : messages.home.running}
      </strong>
      <p className="statHint">
        {state.status === 'ready'
          ? `${state.meta.name} v${state.meta.version}`
          : messages.home.readingServiceInfo}
      </p>
    </article>
  )
}
