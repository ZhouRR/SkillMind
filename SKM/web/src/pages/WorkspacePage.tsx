import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'

import {
  cancelRun,
  loadProjectModules,
  loadProjectTasks,
  loadRun,
  loadRunDetail,
  subscribeRunEvents,
  type ProjectModuleRecord,
  type PublishedTaskRecord,
  type RunEventRecord,
  type RunRecord,
  type RunDetailRecord,
  type RespondedInteractionRecord,
  type RunStatus,
} from '../api'
import { EmptyState, EventTimelineItem, ModalDialog, PageHeader, StatusBadge } from '../components/PageElements'
import { AgentConversation } from '../components/AgentConversation'
import { RunResultPanel, type RunDetailState } from '../components/RunResultPanel'
import { RunSubmissionPanel } from '../components/RunSubmissionPanel'
import { TaskLaunchFields } from '../components/TaskLaunchFields'
import { useRunSubmission } from '../hooks/useRunSubmission'
import type { SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import type { UiMessages } from '../lib/i18n/messages'
import { type AgentPromptSummary } from '../lib/agentStream'
import { routeHref } from '../lib/routing'
import { formatLocalTimestamp } from '../lib/presentation'
import { applicableRunSnapshot } from '../lib/runReplay'
import { applyInteractionSnapshot, interactionAccessFailure, interactionFailure, sameInteractionIdentity } from '../lib/interactionResponse'
import { submissionPayload, type FrozenRunSubmission } from '../lib/runSubmission'
import {
  buildTaskDraft,
  defaultSourceProviders,
  filterTasksByModule,
  taskCatalogId,
} from '../lib/taskDraft'

const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED'])

/** Run 操作と SSE 接続の利用者向け state。 */
type RunUiState = 'idle' | 'cancelling' | 'streaming' | 'reconnecting' | 'error'

/** 現在 Run の観測タブ (会話・結果・監査)。同時に一つだけ表示する。 */
type ObservationTab = 'conversation' | 'result' | 'events'

/** 現在 Project の task 実行、状態、監査 event を表示する標準 Workspace 画面。
 *
 * Project 切替と業務模块の選択はどちらも sidebar が持つ(moduleId 空は模块を持たない Project)。
 */
export function WorkspacePage(props: WorkspacePageProps) {
  return <WorkspaceContent key={JSON.stringify([props.actorId, props.projectId.toLowerCase()])} {...props} />
}

/** 作成は actor/Project、普通答復と読取は Session 世代も含めて所有権を分ける。 */
interface WorkspacePageProps {
  actorId: string
  projectId: string
  moduleId: string
  csrfToken: string
  projectReadOnly?: boolean
  /** Home/Task/History から渡された deep-link context。 */
  initialRunId?: string | null
  initialTaskId?: string | null
  onSessionExpired?: SessionEnded
}

/** 実 App の guard を持たない単独 harness/読み取り利用向けの無作用 callback。 */
const ignoreSessionExpired: SessionEnded = () => {}

/** 作成草稿を保持しつつ、現在 Run の観測と普通答復を Session ごとに再検証する。 */
function WorkspaceContent({ actorId, projectId, moduleId, csrfToken, initialRunId = null, initialTaskId = null,
  projectReadOnly = false,
  onSessionExpired = ignoreSessionExpired }: WorkspacePageProps) {
  const messages = useMessages()
  const [tasks, setTasks] = useState<PublishedTaskRecord[]>([])
  const [modules, setModules] = useState<ProjectModuleRecord[]>([])
  const [tasksError, setTasksError] = useState<string | null>(null)
  const [selectedTaskId, setSelectedTaskId] = useState<string>('')
  const [inputText, setInputText] = useState<string>('{}')
  const [sourceProviders, setSourceProviders] = useState<Record<string, string>>({})
  const [run, setRun] = useState<RunRecord | null>(null)
  const runRef = useRef(run)
  runRef.current = run
  const [events, setEvents] = useState<RunEventRecord[]>([])
  const [uiState, setUiState] = useState<RunUiState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [promptSummary, setPromptSummary] = useState<AgentPromptSummary | null>(null)
  const [detailState, setDetailState] = useState<RunDetailState>({ status: 'idle' })
  const detailRef = useRef(detailState)
  detailRef.current = detailState
  const sessionIdentity = useMemo(() => ({}), [csrfToken])
  const currentSessionIdentity = useRef(sessionIdentity)
  currentSessionIdentity.current = sessionIdentity
  const detailSessionIdentity = useRef(sessionIdentity)
  const [observationTab, setObservationTab] = useState<ObservationTab>('conversation')
  const [selectionRevision, setSelectionRevision] = useState(0)
  const [runDialogOpen, setRunDialogOpen] = useState(false)
  const [acknowledgePrevious, setAcknowledgePrevious] = useState(false)
  const submission = useRunSubmission({ actorId, projectId, csrfToken, onConfirmed: handleRunConfirmed })
  const tasksController = useRef<AbortController | null>(null)
  const modulesController = useRef<AbortController | null>(null)
  const refreshController = useRef<AbortController | null>(null)
  const cancelController = useRef<AbortController | null>(null)
  const detailController = useRef<AbortController | null>(null)
  const initialRunController = useRef<AbortController | null>(null)
  const appliedInitialRunId = useRef<string | null>(null)
  const appliedInitialTaskId = useRef<string | null>(null)
  const initializedSourceTask = useRef<string | null>(null)
  const selectingInitialRun = initialRunId !== null && appliedInitialRunId.current !== initialRunId
  const runIdentity = useMemo(() => ({}), [run?.run_id, initialRunId, sessionIdentity])
  const currentRunIdentity = useRef(runIdentity)
  currentRunIdentity.current = runIdentity
  const mounted = useRef(false)

  /** token が A→B→A と戻っても、以前の Session closure を現在として扱わない。 */
  function ownsSession(): boolean {
    return mounted.current && currentSessionIdentity.current === sessionIdentity
  }
  const auditEvents = useMemo(
    () => events.filter((event) => event.event_type !== 'TEXT_DELTA'),
    [events],
  )
  const selectedTask = useMemo(
    () => tasks.find((task) => taskCatalogId(task) === selectedTaskId) ?? null,
    [tasks, selectedTaskId],
  )
  const activeModule = useMemo(
    () => modules.find((module) => module.module_id === moduleId) ?? null,
    [modules, moduleId],
  )
  // sidebar で選択された業務模块に task 一覧を絞る。module を持たない Project(null)は catalog 全件。
  const visibleTasks = useMemo(() => filterTasksByModule(tasks, activeModule), [tasks, activeModule])

  useEffect(() => setAcknowledgePrevious(false), [submission.pending?.request.idempotencyKey, submission.pending?.phase])

  useLayoutEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      tasksController.current?.abort()
      modulesController.current?.abort()
      refreshController.current?.abort()
      cancelController.current?.abort()
      detailController.current?.abort()
      initialRunController.current?.abort()
    }
  }, [sessionIdentity])

  // 普通答復の資格は新 Session で読み直す。作成弹窗/草稿/原作成 key は独立して保持する。
  useLayoutEffect(() => {
    if (detailSessionIdentity.current === sessionIdentity) return
    setEvents([])
    setDetailState({ status: run ? 'loading' : 'idle' })
  }, [sessionIdentity])

  // Project ごとに published task catalog を取得し、任意 task を通用 form で実行できるようにする。
  // projectId は sidebar/项目管理の検証済み選択に限られるため、未選択（空）だけを弾けばよい。
  useEffect(() => {
    tasksController.current?.abort()
    if (!projectId) {
      setTasks([])
      setTasksError(messages.workspace.selectProjectFirst)
      return
    }
    const controller = new AbortController()
    tasksController.current = controller
    setTasksError(null)
    void loadProjectTasks(projectId, controller.signal)
      .then((catalog) => { if (!controller.signal.aborted && ownsSession()) setTasks(catalog.tasks) })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted && ownsSession()) {
          setTasks([])
          setTasksError(caught instanceof Error ? caught.message : 'Unknown task catalog API error')
        }
      })
    return () => controller.abort()
  }, [projectId, sessionIdentity])

  // Project の module 構成を取得し、任務を業務単位で見せる。失敗は task 実行を妨げない。
  useEffect(() => {
    modulesController.current?.abort()
    if (!projectId) {
      setModules([])
      return
    }
    const controller = new AbortController()
    modulesController.current = controller
    void loadProjectModules(projectId, controller.signal)
      .then((loaded) => { if (!controller.signal.aborted && ownsSession()) setModules(loaded) })
      .catch(() => {
        if (!controller.signal.aborted && ownsSession()) setModules([])
      })
    return () => controller.abort()
  }, [projectId, sessionIdentity])

  // 選択 task は常に表示中一覧の中へ丸める。模块切替・catalog 再読込の直後も実行可能な既定を保ち、
  // 選択が実際に変わった時だけ入力を初期化する(手動選択の onChange と同じ規約)。
  useEffect(() => {
    if (
      visibleTasks.some((task) => taskCatalogId(task) === selectedTaskId)
      || (initialTaskId !== null && visibleTasks.some((task) => taskCatalogId(task) === initialTaskId))
    ) return
    setSelectedTaskId(visibleTasks[0] ? taskCatalogId(visibleTasks[0]) : '')
    setInputText('{}')
  }, [visibleTasks, selectedTaskId])

  // 「立即执行」链接直接打开对应 task 的 modal，避免先回到工作空间再重复寻找 task。
  useEffect(() => {
    if (!initialTaskId || appliedInitialTaskId.current === initialTaskId) return
    if (!visibleTasks.some((task) => taskCatalogId(task) === initialTaskId)) return
    appliedInitialTaskId.current = initialTaskId
    setSelectedTaskId(initialTaskId)
    setInputText('{}')
    setError(null)
    setRunDialogOpen(true)
  }, [initialTaskId, visibleTasks])

  // 文書は必ず明示選択する。同じ task の候補更新では草稿を保持し、失効を選択欄で説明する。
  useEffect(() => {
    const task = tasks.find((item) => taskCatalogId(item) === selectedTaskId)
    if (!task) {
      initializedSourceTask.current = null
      setSourceProviders({})
      return
    }
    const context = `${projectId}:${selectedTaskId}`
    if (initializedSourceTask.current === context) return
    initializedSourceTask.current = context
    setSourceProviders(defaultSourceProviders(task))
  }, [projectId, selectedTaskId, tasks])

  useEffect(() => {
    if (!run || (TERMINAL_STATUSES.has(run.status) && events.length > 0)) return
    setUiState('streaming')
    const after = auditEvents.at(-1)?.sequence ?? 0
    let subscription: ReturnType<typeof subscribeRunEvents> | null = null
    let active = true
    subscription = subscribeRunEvents(
      run.run_id,
      after,
      (incoming) => {
        if (!mounted.current || !active || currentRunIdentity.current !== runIdentity) return
        setEvents((current) => appendEvent(current, incoming))
        setUiState('streaming')
        const snapshot = applicableRunSnapshot(incoming, run.row_version)
        if (snapshot !== null) {
          setRun((current) => current && snapshot.rowVersion >= current.row_version
            ? { ...current, status: snapshot.status, row_version: snapshot.rowVersion }
            : current)
          if (TERMINAL_STATUSES.has(snapshot.status)) {
            subscription?.close()
            setUiState('idle')
          }
        }
      },
      () => { if (mounted.current && active && currentRunIdentity.current === runIdentity) setUiState('reconnecting') },
    )
    return () => { active = false; subscription?.close() }
    // Subscription の再作成は Run identity と終態化だけに限定し、event 追加による再接続を防ぐ。
  }, [run?.run_id, run?.status, selectionRevision, runIdentity])

  // Home/履歴画面から渡された run ID は直接正本 snapshot へ解決する。
  // Workspace に一覧用の history API を重ねないことで、実行観測と履歴探索の責務を分ける。
  useEffect(() => {
    initialRunController.current?.abort()
    if (!initialRunId || !projectId || appliedInitialRunId.current === initialRunId) return
    const controller = new AbortController()
    initialRunController.current = controller
    void loadRun(initialRunId, controller.signal)
      .then((loaded) => {
        if (controller.signal.aborted || !ownsSession() || currentRunIdentity.current !== runIdentity) return
        if (!sameInteractionIdentity(loaded.project_id, projectId)
          || !sameInteractionIdentity(loaded.run_id, initialRunId)) throw new Error('Run scope mismatch')
        appliedInitialRunId.current = initialRunId
        setEvents([])
        setDetailState({ status: 'idle' })
        setObservationTab('conversation')
        setPromptSummary({
          taskTitle: messages.elements.runFallbackTitle(shortRunId(loaded.run_id)),
          capability: null,
          input: {},
          sources: {},
        })
        setRun(loaded)
        setUiState(TERMINAL_STATUSES.has(loaded.status) ? 'idle' : 'streaming')
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted && ownsSession() && currentRunIdentity.current === runIdentity) {
          if (interactionFailure(caught, false).key === 'sessionExpired') onSessionExpired()
          setError(messages.home.loadRunsFailed)
        }
      })
    return () => controller.abort()
  }, [initialRunId, messages.elements.runFallbackTitle, messages.home.loadRunsFailed, projectId, sessionIdentity])

  useEffect(() => {
    if (!run) return
    detailController.current?.abort()
    const controller = new AbortController()
    detailController.current = controller
    const sameSession = detailSessionIdentity.current === sessionIdentity
    setDetailState((previous) => ({ status: 'loading', detail: sameSession ? retainedRunDetail(previous, run) : undefined }))
    void loadRunDetail(run.project_id, run.run_id, controller.signal)
      .then((detail) => {
        if (controller.signal.aborted || !ownsSession() || currentRunIdentity.current !== runIdentity) return
        const latestDetail = retainedRunDetail(detailRef.current, run)
        detailSessionIdentity.current = sessionIdentity
        if (detail.row_version < Math.max(runRef.current?.row_version ?? 0, latestDetail?.row_version ?? 0)) {
          setDetailState((previous) => ({ status: 'error', detail: retainedRunDetail(previous, run), message: messages.interactionResponse.failures.loadFailed }))
          return
        }
        setDetailState({ status: 'ready', detail })
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted && ownsSession() && currentRunIdentity.current === runIdentity) {
          const failure = interactionFailure(caught, false)
          if (failure.key === 'sessionExpired') onSessionExpired()
          detailSessionIdentity.current = sessionIdentity
          setDetailState((previous) => ({
            status: 'error',
            detail: sameSession ? retainedRunDetail(previous, run) : undefined,
            message: messages.interactionResponse.failures[failure.key],
            accessFailure: interactionAccessFailure(failure) ?? undefined,
          }))
        }
      })
    return () => controller.abort()
  }, [run?.project_id, run?.run_id, run?.row_version, selectionRevision, sessionIdentity])

  // 終態または user interaction 待機では詳細へ寄せ、実行中は会話タブの streaming を維持する。
  useEffect(() => {
    if (
      detailState.status === 'ready'
      && (TERMINAL_STATUSES.has(detailState.detail.status)
        || detailState.detail.status === 'WAITING_FOR_INPUT'
        || detailState.detail.status === 'WAITING_FOR_APPROVAL')
    ) setObservationTab('result')
  }, [detailState])

  /** 新しい意図を固定する。未確認要求の再送はここを通らず、元の本文を使う。 */
  function handleCreate(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    if (submission.pending !== null && (!acknowledgePrevious || submission.pending.phase === 'sending')) return
    if (!selectedTask) {
      setError(messages.workspace.selectTaskFirst)
      return
    }
    const draft = buildTaskDraft(selectedTask, inputText, sourceProviders)
    if (draft === null) {
      setError(messages.workspace.documentSelection.incompleteDraft)
      return
    }
    setError(null)
    try {
      submission.start(draft, selectedTask.capability, acknowledgePrevious)
    } catch {
      setError(messages.workspace.submission.unavailable)
    }
  }

  /** 初回/再送で確認した Run を既存の観測経路に載せる。未確認中は前の Run を消さない。 */
  function handleRunConfirmed(request: FrozenRunSubmission, created: RunRecord): void {
    refreshController.current?.abort()
    cancelController.current?.abort()
    detailController.current?.abort()
    initialRunController.current?.abort()
    const payload = submissionPayload(request)
    setEvents([])
    setDetailState({ status: 'idle' })
    setObservationTab('conversation')
    setPromptSummary({
      taskTitle: request.taskTitle,
      capability: request.capability,
      input: payload.input,
      sources: payload.sources,
    })
    setRun(created)
    setUiState(TERMINAL_STATUSES.has(created.status) ? 'idle' : 'streaming')
    setError(null)
    setRunDialogOpen(false)
  }

  /** 入力弹窗を開く。前回の失敗 error を持ち越さない。 */
  function openRunDialog(): void {
    setError(null)
    if (uiState === 'error') setUiState('idle')
    setRunDialogOpen(true)
  }

  /** SSE とは別に PostgreSQL 正本の現在 Run 状態を再取得する。 */
  async function handleRefresh(): Promise<void> {
    if (!run || !ownsSession()) return
    refreshController.current?.abort()
    const controller = new AbortController()
    refreshController.current = controller
    try {
      const refreshed = await loadRun(run.run_id, controller.signal)
      if (controller.signal.aborted || !ownsSession() || currentRunIdentity.current !== runIdentity) return
      setRun((current) => current && sameInteractionIdentity(current.run_id, refreshed.run_id)
        && sameInteractionIdentity(current.project_id, refreshed.project_id)
        && refreshed.row_version >= current.row_version ? refreshed : current)
    } catch (caught: unknown) {
      if (!controller.signal.aborted && ownsSession() && currentRunIdentity.current === runIdentity) {
        setError(caught instanceof Error ? caught.message : 'Unknown API error')
        setUiState('error')
      }
    }
  }

  /** 現在の Run に取消 intent を送り、即時終態なら snapshot も反映する。 */
  async function handleCancel(): Promise<void> {
    if (!run || !ownsSession() || TERMINAL_STATUSES.has(run.status)) return
    cancelController.current?.abort()
    const controller = new AbortController()
    cancelController.current = controller
    setUiState('cancelling')
    setError(null)
    try {
      const cancelled = await cancelRun(run.run_id, csrfToken, controller.signal)
      if (controller.signal.aborted || !ownsSession() || currentRunIdentity.current !== runIdentity) return
      setRun((current) => current && current.run_id === cancelled.run_id
        ? { ...current, status: cancelled.status, row_version: cancelled.row_version }
        : current)
      setUiState(cancelled.status === 'CANCELLED' ? 'idle' : 'streaming')
    } catch (caught: unknown) {
      if (!controller.signal.aborted && ownsSession() && currentRunIdentity.current === runIdentity) {
        setError(caught instanceof Error ? caught.message : 'Unknown cancel API error')
        setUiState('error')
      }
    }
  }

  /** Interaction response で返った正本 snapshot を反映し、次 Segment の SSE と detail を再接続する。 */
  function handleInteractionResponded(response: RespondedInteractionRecord): void {
    setRun((current) => current ? applyInteractionSnapshot(current, response) : current)
    setSelectionRevision((current) => current + 1)
    // 原回执が古くても正常な重放。可視の確認区画を残して現在 detail だけを再取得する。
  }

  /** 原 GET の事実を現在 Run へ投影するが、答復の受理とは扱わず旧 snapshot も戻さない。 */
  const handleInteractionFacts = useCallback((detail: RunDetailRecord): void => {
    const current = runRef.current
    if (!ownsSession() || !current || !sameInteractionIdentity(current.run_id, detail.run_id)
      || !sameInteractionIdentity(current.project_id, detail.project_id)
      || detail.row_version < Math.max(current.row_version, retainedRunDetail(detailRef.current, current)?.row_version ?? 0)) return
    detailSessionIdentity.current = sessionIdentity
    setDetailState((previous) => previous.status === 'ready' && previous.detail === detail
      ? previous : { status: 'ready', detail })
    setRun((previous) => previous && detail.row_version > previous.row_version
      ? { ...previous, status: detail.status, row_version: detail.row_version } : previous)
  }, [sessionIdentity])

  /** Proposal decision 後に PostgreSQL snapshot と SSE/detail 購読を再同期する。 */
  function handleProposalDecided(): void {
    setSelectionRevision((current) => current + 1)
    setObservationTab('conversation')
    setUiState('streaming')
    void handleRefresh()
  }

  return (
    <>
      <PageHeader
        title={activeModule ? messages.workspace.titleWithModule(activeModule.name) : messages.routes.workspace.label}
        description={run ? undefined : activeModule?.description || messages.workspace.description}
        aside={<span className="scopeBadge">{activeModule?.name ?? messages.workspace.projectWideScope}</span>}
      />
      <section className={`workspace${run ? ' workspaceReading' : ''}`} aria-label={messages.workspace.taskExecutionAria}>
        {/* 左 rail は「実行の入口」と履歴へのショートカット、右 main は現在 Run の観測に責務を分離する。
            履歴の検索・ページングは独立画面へ移し、ここでは実行観測を縦に圧迫しない。 */}
        <div className="workspaceRail">
          <section className="panel runLauncher">
            {!run && <div className="panelHeader"><h2>{messages.workspace.newRun}</h2></div>}
            {tasksError && <p className="error" role="alert">{tasksError}</p>}
            {/* 空態は文言だけで終わらせず、解決先(技能库)への入口を同じ行に置く。 */}
            {!tasksError && tasks.length === 0 && (
              <p className="hint">{messages.workspace.noPublishedTasks} <a href={routeHref('skills')}>{messages.projects.goSkills}</a></p>
            )}
            {/* 「全部」入口を廃したので、模块に紐づかない task はここからは見えない。
                空態では原因(未公開/未束縛)と解決先(模块設定)を必ず添える。 */}
            {!tasksError && tasks.length > 0 && visibleTasks.length === 0 && (
              <p className="hint">
                {messages.workspace.noModuleTasks}{' '}
                <a href={routeHref('projects')}>{messages.workspace.goModuleSettings}</a>
              </p>
            )}
            {!run && visibleTasks.length > 0 && (
              <p className="hint">{messages.workspace.newRunIntro} {messages.workspace.runnableCount(visibleTasks.length)}</p>
            )}
            <button
              className={run ? 'secondaryButton' : 'primaryButton'}
              disabled={visibleTasks.length === 0 && submission.pending === null}
              type="button"
              onClick={openRunDialog}
            >
              {submission.pending ? messages.workspace.submission.open : messages.workspace.openNewRun}
            </button>
            {submission.pending && <p className="hint" role="status">{messages.workspace.submission.phase[submission.pending.phase]}</p>}
          </section>

          <section className="panel historyShortcut">
            <div className="panelHeader">
              {!run && <h2>{messages.workspace.history}</h2>}
              <a className="secondaryButton compactButton" href={routeHref('history', projectId)}>
                {messages.routes.history.label}
              </a>
            </div>
            {!run && <p className="hint">{messages.historyPage.description}</p>}
          </section>
        </div>

        <div className="workspaceMain">
          <section className="panel runPanel" aria-live="polite">
            <div className="panelHeader"><h2>{run && promptSummary?.taskTitle !== messages.elements.runFallbackTitle(shortRunId(run.run_id)) ? promptSummary?.taskTitle ?? messages.workspace.runStatus : messages.workspace.runStatus}</h2>{run && <StatusBadge status={run.status} />}</div>
            {run && <time className="runTimestamp" dateTime={run.created_at}>{formatLocalTimestamp(run.created_at)}</time>}
            {!run && (
              <EmptyState
                text={submission.pending ? messages.workspace.submission.phase[submission.pending.phase] : messages.workspace.emptyBeforeRun}
                action={visibleTasks.length > 0 && (
                  <button className="secondaryButton compactButton" type="button" onClick={openRunDialog}>
                    {messages.workspace.openNewRun}
                  </button>
                )}
              />
            )}
            {/* Row version は楽観 lock の実装細部のため表示しない。状態は enum catalog の利用者語で示す。 */}
            {run && <><dl className="runFacts"><div><dt>{messages.workspace.runIdLabel}</dt><dd className="mono" title={run.run_id}>{shortRunId(run.run_id)}</dd></div>{!TERMINAL_STATUSES.has(run.status) && <div><dt>{messages.workspace.connLabel}</dt><dd>{connectionLabel(messages, uiState, run.status)}</dd></div>}</dl>{!TERMINAL_STATUSES.has(run.status) && <div className="formRow"><button className="secondaryButton" type="button" onClick={() => void handleRefresh()}>{messages.workspace.refreshDb}</button><button className="secondaryButton" disabled={uiState === 'cancelling'} type="button" onClick={() => void handleCancel()}>{uiState === 'cancelling' ? messages.workspace.cancelling : messages.workspace.cancelRun}</button></div>}</>}
          </section>

          {/* 会話・結果・監査は同時に一つだけ観測する。runPanel を残し、以下をタブへ束ねて縦の積み上げを解消する。 */}
          <section className="panel observationPanel">
            <div className="tabBar" role="tablist" aria-label={messages.workspace.observationAria}>
              <ObservationTabButton current={observationTab} tab="conversation" onSelect={setObservationTab}>{messages.workspace.tabConversation}</ObservationTabButton>
              <ObservationTabButton current={observationTab} tab="result" onSelect={setObservationTab}>
                {messages.workspace.tabResult}
                {/* 回答・承認待ちの間は結果 tab に点を出し、他 tab からも待ち事項の所在を示す。状態名は tab 隣の StatusBadge が読み上げる。 */}
                {run !== null && (run.status === 'WAITING_FOR_INPUT' || run.status === 'WAITING_FOR_APPROVAL')
                  && <i className="tabAlert" aria-hidden="true" />}
              </ObservationTabButton>
              <ObservationTabButton current={observationTab} tab="events" onSelect={setObservationTab}>
                {messages.workspace.tabEvents}<span className="eventCount">{auditEvents.length}</span>
              </ObservationTabButton>
            </div>
            <div className="tabPanel" role="tabpanel">
              {observationTab === 'conversation'
                && <AgentConversation prompt={promptSummary} events={events} runStatus={run?.status ?? null} />}
              <div className="workspaceResultMount" hidden={observationTab !== 'result'}>
                <RunResultPanel
                  actorId={actorId}
                  csrfToken={csrfToken}
                  projectReadOnly={projectReadOnly}
                  projectId={projectId}
                  runId={selectingInitialRun ? '' : run?.run_id ?? ''}
                  onSessionExpired={onSessionExpired}
                  onInteractionResponded={handleInteractionResponded}
                  onInteractionFacts={handleInteractionFacts}
                  onProposalDecided={handleProposalDecided}
                  state={selectingInitialRun || detailSessionIdentity.current !== sessionIdentity ? { status: 'loading' } : detailState}
                />
              </div>
              {observationTab === 'events' && (
                auditEvents.length === 0
                  ? <EmptyState text={messages.workspace.sseEmpty} />
                  : <ol className="timeline">{auditEvents.map((item) => <EventTimelineItem key={item.sequence} event={item} />)}</ol>
              )}
            </div>
          </section>
        </div>
      </section>
      {/* 新規実行の入力(任務・就緒度・来源・入力)は幅の広い弹窗で行い、rail の圧縮表示をやめる。 */}
      <ModalDialog open={runDialogOpen} title={messages.workspace.newRun} wide onClose={() => setRunDialogOpen(false)}>
        <form className="runForm" onSubmit={handleCreate}>
          {submission.pending && (
            <RunSubmissionPanel
              pending={submission.pending}
              acknowledgePrevious={acknowledgePrevious}
              onAcknowledge={setAcknowledgePrevious}
              onRetry={submission.retry}
            />
          )}
          {visibleTasks.length === 0 && <p className="hint">{messages.workspace.noModuleTasks}</p>}
          {visibleTasks.length > 0 && (
            <>
              <label>{messages.workspace.taskLabel}
                <select value={selectedTaskId} onChange={(event) => { setSelectedTaskId(event.target.value); setInputText('{}') }}>
                  {visibleTasks.map((task) => (
                    <option key={taskCatalogId(task)} value={taskCatalogId(task)}>{task.title} · {task.skill_name} v{task.version}</option>
                  ))}
                </select>
              </label>
              <TaskLaunchFields
                inputText={inputText}
                onInputTextChange={setInputText}
                onSourceChange={(key, value) => setSourceProviders((current) => ({ ...current, [key]: value }))}
                sourceProviders={sourceProviders}
                task={selectedTask}
              />
              <button
                className="primaryButton"
                disabled={!selectedTask || (submission.pending !== null && (submission.pending.phase === 'sending' || !acknowledgePrevious))}
                type="submit"
              >
                {submission.pending?.phase === 'sending'
                  ? messages.workspace.creating
                  : submission.pending ? messages.workspace.submission.startNew : messages.workspace.startRun}
              </button>
            </>
          )}
          {error && <p className="error" role="alert">{error}</p>}
        </form>
      </ModalDialog>
    </>
  )
}

/** 再取得中の過去表示は同じ Run のものに限定し、現在の書込可否には使わない。 */
function retainedRunDetail(state: RunDetailState, run: RunRecord): RunDetailRecord | undefined {
  const detail = 'detail' in state ? state.detail : undefined
  return detail && sameInteractionIdentity(detail.project_id, run.project_id)
    && sameInteractionIdentity(detail.run_id, run.run_id) ? detail : undefined
}

/** 単一の観測タブ button。選択状態を aria-selected で表し、tablist 内で切り替える。 */
function ObservationTabButton({ current, tab, onSelect, children }: {
  current: ObservationTab
  tab: ObservationTab
  onSelect: (tab: ObservationTab) => void
  children: ReactNode
}) {
  return (
    <button
      aria-selected={current === tab}
      className="tab"
      onClick={() => onSelect(tab)}
      role="tab"
      type="button"
    >
      {children}
    </button>
  )
}

/** Duplicate replay を sequence で排除し、常に昇順を保つ。 */
function appendEvent(current: RunEventRecord[], incoming: RunEventRecord): RunEventRecord[] {
  if (current.some((event) => event.sequence === incoming.sequence)) return current
  return [...current, incoming].sort((left, right) => left.sequence - right.sequence)
}

/** SSE UI state と terminal status から利用者向け表示を返す。 */
function connectionLabel(messages: UiMessages, state: RunUiState, status: RunStatus): string {
  if (TERMINAL_STATUSES.has(status)) return messages.workspace.connFinished
  if (state === 'reconnecting') return messages.workspace.connReconnecting
  if (state === 'streaming') return messages.workspace.connStreaming
  return messages.workspace.connIdle
}

/** UUID は全文を常時表示せず、title へ完全値を残して識別だけを可能にする。 */
function shortRunId(value: string): string {
  return value.slice(0, 8)
}

/** Run が稼働中から終態へ初めて遷移したかを判定する pure logic。 */
export function shouldRefreshRunHistory(previous: RunStatus, current: RunStatus): boolean {
  return !TERMINAL_STATUSES.has(previous) && TERMINAL_STATUSES.has(current)
}
