import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import type { ProjectState } from '../appState'
import type { ProjectRecord, RunEventRecord, RunStatus } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTime } from '../lib/presentation'

/** 画面の目的と補助情報を統一した compact heading として表示する。 */
export function PageHeader({ title, description, aside }: {
  title: string
  description: string
  aside?: ReactNode
}) {
  return (
    <header className="pageHeader">
      <div><h1>{title}</h1><p className="pageDescription">{description}</p></div>
      {aside && <div className="pageActions">{aside}</div>}
    </header>
  )
}

/** 共有 Project context を名称で選択する。UUID の手入力を廃し、選択中の ID は補助行に降格する。 */
export function ProjectContextSelect({ projectState, projectId, currentProject, onSelect, onRefresh, label }: {
  projectState: ProjectState
  projectId: string
  /** 精確読取で認可済みの現 Project。一覧の成功・失敗とは独立して扱う。 */
  currentProject?: ProjectRecord | null
  onSelect: (projectId: string) => void
  /** 読取を再試行しても、明示された対象や権限判断を selector 自身では変えない。 */
  onRefresh?: () => void
  label?: string
}) {
  const messages = useMessages()
  const resolvedLabel = label ?? messages.elements.projectLabel
  const listed = projectState.status === 'ready' ? projectState.projects : []
  const candidates = new Map(listed.map((project) => [project.project_id, project] as const))
  // 詳細読取は一覧完了の証拠ではない。現対象だけを補い、一覧の真の状態は維持する。
  if (currentProject?.project_id === projectId) candidates.set(projectId, currentProject)
  const projects = [...candidates.values()]
  const selectedProject = projects.find((project) => project.project_id === projectId)
  // 未解決の value を native select に渡すと先頭候補へ見かけ上切り替わるため、
  // 元の対象を表す option を残し、別 Project は明示的に選ばせる。
  const needsPlaceholder = !selectedProject
  const placeholder = projectState.status === 'ready'
    ? (projectId !== ''
      ? messages.elements.projectUnavailable
      : projects.length === 0
        ? messages.elements.noAccessibleProjects
        : messages.elements.selectProject)
    : projectState.status === 'error'
      ? messages.elements.projectListFailed
      : messages.elements.loadingProjects
  return (
    <div className="projectContext">
      <label>{resolvedLabel}
        <select
          disabled={projects.length === 0}
          value={projectId}
          onChange={(event) => onSelect(event.target.value)}
        >
          {needsPlaceholder && <option disabled value={projectId}>{placeholder}</option>}
          {projects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.name} · {project.key}
              {project.status === 'ARCHIVED' && ` · ${messages.elements.archivedProject}`}
            </option>
          ))}
        </select>
      </label>
      {selectedProject && (projectState.status === 'idle' || projectState.status === 'loading') && (
        <p className="muted" role="status">{messages.elements.loadingProjects}</p>
      )}
      {projectState.status === 'error' && <p className="error" role="alert">{projectState.message}</p>}
      {projectState.status === 'error' && onRefresh && (
        <button className="secondaryButton compactButton" onClick={onRefresh} type="button">{messages.runHistory.retry}</button>
      )}
      {projectId !== '' && <p className="projectContextId" title={projectId}>{projectId}</p>}
    </div>
  )
}

/** Run status を一貫した color token 付き badge で表示する。


    文言は利用者言語の enum catalog から引き、CSS class は契約値のまま保つ(色 token の安定)。
    catalog 未収録の新契約値は原文 fallback で表示し、画面を壊さない。 */
export function StatusBadge({ status }: { status: RunStatus }) {
  const messages = useMessages()
  return (
    <span className={`statusBadge status-${status.toLowerCase()}`}>
      {messages.enums.runStatus[status] ?? status}
    </span>
  )
}

/** Panel が未着手の理由を空白にせず表示する。glyph は装飾で、意味は文言だけが持つ。 */
export function EmptyState({ text, action }: { text: string; action?: ReactNode }) {
  return (
    <div className="emptyState">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M3.5 13.5h4l2 2.5h5l2-2.5h4" />
        <path d="M5.7 5.5h12.6l2.2 8v4a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5v-4Z" />
      </svg>
      <p>{text}</p>
      {action && <div className="emptyStateAction">{action}</div>}
    </div>
  )
}

/** 長い form や全画面 preview を一覧・観測画面から切り離す共通 modal。
 *
 *  常時 mount + hidden 切替とする:頁面測試(renderToStaticMarkup + toContain)が
 *  中身を検証できる状態を保ち、条件描画による断言切れを防ぐ。
 *  開いている間は Escape と遮罩 click で閉じられ、背面の scroll を止め、
 *  閉じた後は開いた時の要素へ焦点を戻す(keyboard 利用者が現在地を失わないため)。 */
export function ModalDialog({ open, title, meta, actions, wide = false, onClose, children }: {
  open: boolean
  title: string
  /** 見出し横の補助情報(寸法・種別など)。 */
  meta?: ReactNode
  /** 閉じる button の手前に置く固有操作(download など)。 */
  actions?: ReactNode
  wide?: boolean
  onClose: () => void
  children: ReactNode
}) {
  const messages = useMessages()
  const dialogRef = useRef<HTMLDivElement>(null)
  const restoreFocusTo = useRef<HTMLElement | null>(null)
  const closeRef = useRef(onClose)
  useEffect(() => { closeRef.current = onClose }, [onClose])
  useEffect(() => {
    if (!open) return
    // 入力・言語・送信状態で onClose の参照が変わっても、編集中の焦点を奪わない。
    // callback だけを最新化し、focus/scroll の lifecycle は開閉に限定する。
    // 開く直前の焦点を控え、dialog 自体へ移す(Escape と scroll を直ちに効かせる)。
    restoreFocusTo.current = window.document.activeElement as HTMLElement | null
    dialogRef.current?.focus()
    const handleKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') closeRef.current()
    }
    window.addEventListener('keydown', handleKey)
    // 背面の scroll を止める。閉じたら元の値へ戻し、他所の overflow 指定を壊さない。
    const body = window.document.body
    const previousOverflow = body.style.overflow
    body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', handleKey)
      body.style.overflow = previousOverflow
      restoreFocusTo.current?.focus()
    }
  }, [open])
  return (
    <div
      className={wide ? 'modalOverlay modalWide' : 'modalOverlay'}
      hidden={!open}
      onClick={(event) => { if (event.target === event.currentTarget) onClose() }}
    >
      <div
        aria-label={title}
        aria-modal="true"
        className="modalDialog"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="modalHeader">
          <strong title={title}>{title}</strong>
          {meta && <small className="modalMeta">{meta}</small>}
          <div className="modalHeaderActions">
            {actions}
            <button className="secondaryButton compactButton" type="button" onClick={onClose}>{messages.elements.close}</button>
          </div>
        </div>
        <div className="modalBody">{children}</div>
      </div>
    </div>
  )
}

/** 破壊的操作の実行前確認。文言は呼び出し元が用意する。 */
export interface ConfirmRequest {
  /** dialog の見出し。実行する操作名を短く置く。 */
  title: string
  /** 何が起きるか・取り消せるかを説明する本文。改行はそのまま表示する。 */
  message: string
  /** 実行 button の label(「削除」「廃止」など操作名を再掲する)。 */
  confirmLabel: string
  /** 取り消せない操作は true。実行 button を danger 色の実心にする。 */
  destructive?: boolean
}

/** 確認 dialog を Promise で待てるようにする hook。
 *
 *  `window.confirm` は同期で真偽を返すため呼び出し側が素直に書けたが、browser 標準の
 *  明色 dialog が暗色 UI から浮き、理由の説明も持てなかった。同じ書き味を保つため
 *  `await confirm(...)` で真偽を受け取れる形にし、描画は返り値の `confirmDialog` に任せる。 */
export function useConfirmDialog(): {
  confirm: (request: ConfirmRequest) => Promise<boolean>
  confirmDialog: ReactNode
} {
  const [request, setRequest] = useState<ConfirmRequest | null>(null)
  const settleRef = useRef<((confirmed: boolean) => void) | null>(null)

  const confirm = useCallback((next: ConfirmRequest): Promise<boolean> => {
    // 前の問い合わせが未解決のまま重ねられた場合は、宙吊りを避けて false で閉じる。
    settleRef.current?.(false)
    setRequest(next)
    return new Promise<boolean>((resolve) => { settleRef.current = resolve })
  }, [])

  const settle = useCallback((confirmed: boolean): void => {
    settleRef.current?.(confirmed)
    settleRef.current = null
    setRequest(null)
  }, [])

  // 解決を待つ側が残ったまま画面が消えると Promise が永久に未解決になる。
  useEffect(() => () => settleRef.current?.(false), [])

  return {
    confirm,
    confirmDialog: (
      <ConfirmDialog request={request} onCancel={() => settle(false)} onConfirm={() => settle(true)} />
    ),
  }
}

/** 確認 dialog 本体。ModalDialog と同じく常時 mount + hidden で描画する。 */
export function ConfirmDialog({ request, onConfirm, onCancel }: {
  request: ConfirmRequest | null
  onConfirm: () => void
  onCancel: () => void
}) {
  const messages = useMessages()
  return (
    <ModalDialog
      open={request !== null}
      title={request?.title ?? ''}
      onClose={onCancel}
    >
      <p className="confirmMessage">{request?.message ?? ''}</p>
      <div className="confirmActions">
        <button className="secondaryButton" type="button" onClick={onCancel}>{messages.elements.cancel}</button>
        <button
          autoFocus
          className={request?.destructive ? 'destructiveButton' : 'primaryButton'}
          type="button"
          onClick={onConfirm}
        >
          {request?.confirmLabel ?? ''}
        </button>
      </div>
    </ModalDialog>
  )
}

/** 一覧の読み込み中に文言ではなく行 placeholder を光らせ、確定 layout を先に見せる。 */
export function LoadingSkeleton({ rows = 3, label }: { rows?: number; label?: string }) {
  const messages = useMessages()
  return (
    <div className="skeletonList" role="status" aria-label={label ?? messages.elements.loading}>
      {Array.from({ length: rows }, (_, index) => <div className="skeletonRow" key={index} />)}
    </div>
  )
}

/** 一つの監査 event を summary と展開可能な payload 詳細として表示する。 */
export function EventTimelineItem({ event }: { event: RunEventRecord }) {
  const messages = useMessages()
  return (
    <li>
      <details className="eventDetails">
        <summary>
          <span className="sequence">#{event.sequence}</span>
          <span className="eventSummary"><strong>{friendlyEventType(messages.enums.runEvent, event.event_type)}</strong><time>{formatLocalTime(event.occurred_at)}</time></span>
          <span className="detailHint">{messages.elements.detail}</span>
        </summary>
        <div className="eventDetailBody">
          <dl className="eventIdentity">
            <div><dt>{messages.elements.eventType}</dt><dd>{event.event_type}</dd></div>
            <div><dt>{messages.elements.eventRun}</dt><dd>{event.run_id}</dd></div>
            <div><dt>{messages.elements.eventAttempt}</dt><dd>{event.run_attempt_id ?? '—'}</dd></div>
            <div><dt>{messages.elements.eventSession}</dt><dd>{event.agent_session_id ?? '—'}</dd></div>
            <div><dt>{messages.elements.eventOccurredAt}</dt><dd>{event.occurred_at}</dd></div>
            <div><dt>{messages.elements.eventTrace}</dt><dd>{event.trace_id ?? '—'}</dd></div>
          </dl>
          <div className="payloadBlock"><span>{messages.elements.eventPayload}</span><pre>{JSON.stringify(event.payload, null, 2)}</pre></div>
        </div>
      </details>
    </li>
  )
}

/** 技術的な event code を監査詳細へ残し、タイムラインの見出しだけを利用者語へ変換する。 */
function friendlyEventType(labels: Record<string, string>, eventType: string): string {
  const normalized = eventType.toUpperCase().replace(/[.-]/g, '_')
  return labels[eventType] ?? labels[normalized] ?? eventType
}
