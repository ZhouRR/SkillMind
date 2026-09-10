import { useCallback, useEffect, useLayoutEffect, useRef, type FormEvent } from 'react'

import { loadProjects, type AuthSessionRecord, type ProjectRecord } from '../api'
import type { ProjectState } from '../appState'
import { useProjectManagement } from '../hooks/useProjectManagement'
import { useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { PROJECT_REQUEST_POLICY } from '../lib/projectFeedback'
import { validProjectDraft, type ProjectDraft, type ProjectIntent } from '../lib/projectManagement'
import { routeHref } from '../lib/routing'
import { EmptyState, LoadingSkeleton } from './PageElements'
import { ProjectDraftFacts, ProjectFacts, ProjectIntentConfirmation, ProjectResponseNotice } from './ProjectManagementElements'
import { ProjectManagementReview } from './ProjectManagementReview'
import '../styles/project-management.css'

/** Platform 管理は選択 Project の資格に依存せず、原 session と context で隔離する。 */
export interface ProjectManagementProps {
  tab: 'projects' | 'archived' | 'modules' | 'members'
  projectId: string
  projectState: ProjectState
  session: AuthSessionRecord
  onSessionEnded: SessionEnded
  setProjectId: (id: string) => void
  onProjectChanged: (project: ProjectRecord) => void
  onProjectArchived: (project: ProjectRecord) => void
  onProjectDeleted?: (project: ProjectRecord) => void
  onEditTab: () => void
}

/** USER は既存の可視一覧だけを表示し、管理用の全量 request は生成しない。 */
export function ProjectManagementPanel(props: ProjectManagementProps) {
  const messages = useMessages()
  if (props.session.user.system_role === 'ADMIN') return <AdminProjectManagement {...props} />
  const projects = props.projectState.status === 'ready' ? props.projectState.projects : []
  return <section className="projectManagementLayout tabPanel" id="project-panel-projects" aria-labelledby="project-tab-projects" role="tabpanel" hidden={props.tab !== 'projects'}>
    <section className="panel"><h2>{messages.projects.accessible}</h2>
      {props.projectState.status === 'loading' && <LoadingSkeleton label={messages.projects.loadingList} rows={3} />}
      {props.projectState.status === 'error' && <ProjectResponseNotice failure={{ key: 'loadFailed' }} />}
      {props.projectState.status === 'ready' && !projects.length && <EmptyState text={messages.projects.emptyActive} />}
      <ProjectRows projects={projects} projectId={props.projectId} locked admin={false} onSelect={props.setProjectId} onAction={() => {}} />
    </section>
    <aside className="panel projectSidePanel"><h2>{messages.projects.permissionsTitle}</h2><p className="hint">{messages.projects.permissionsHint}</p><ProjectLinks /></aside>
  </section>
}

/** 活動/帰档は同じ完全一覧を投影し、CRUD 全部の門禁と原草稿を tab の外に置く。 */
function AdminProjectManagement(props: ProjectManagementProps) {
  const messages = useMessages()
  const listLoader = useCallback((signal: AbortSignal) => loadProjects(true, signal), [])
  const list = useResourceQuery('project-management-list', listLoader, props.onSessionEnded, PROJECT_REQUEST_POLICY)
  const formHeading = useRef<HTMLHeadingElement>(null)
  const archivedHeading = useRef<HTMLHeadingElement>(null)
  const visibleTab = useRef(props.tab)
  visibleTab.current = props.tab
  /** 消える確認 button に focus を残さず、現在見えている管理区画へ戻す。 */
  function focusLanding(): void {
    if (visibleTab.current === 'projects') formHeading.current?.focus()
    if (visibleTab.current === 'archived') archivedHeading.current?.focus()
  }
  useLayoutEffect(() => {
    // 新 context 自身で失われた焦点を受け取り、既存 navigation の焦点は奪わない。
    if (document.activeElement === document.body || document.activeElement === document.documentElement) focusLanding()
  }, [])
  const manager = useProjectManagement({ session: props.session, onSessionEnded: props.onSessionEnded,
    onSaved: (action, project) => {
      list.refresh()
      if (action === 'delete') props.onProjectDeleted?.(project)
      else if (action === 'archive') props.onProjectArchived(project)
      else props.onProjectChanged(project)
      if (action === 'create') props.setProjectId(project.project_id)
      focusLanding()
    } })
  const ready = !list.pending && !list.failure && list.data !== null
  // Shell の初期一覧は読取中の表示だけに使い、操作対象の最新版本には使わない。
  const fallback = list.completed < 0 && !list.failure && props.projectState.status === 'ready' ? props.projectState.projects : []
  const projects = ready ? list.data ?? [] : fallback
  const active = projects.filter((project) => project.status === 'ACTIVE')
  const archived = ready ? projects.filter((project) => project.status === 'ARCHIVED') : []
  useEffect(() => {
    if (manager.editor.original && !manager.intent) formHeading.current?.focus()
  }, [manager.editor.original, manager.intent])

  /** 他の原要求がある間は対象を差し替えず、編集選択時だけ form tab を開く。 */
  function action(actionName: Exclude<ProjectIntent['action'], 'create'>, project: ProjectRecord): void {
    if (!ready) return
    if (actionName === 'edit') {
      if (manager.edit(project)) { props.onEditTab(); formHeading.current?.focus() }
    } else manager.choose(actionName, project)
  }
  /** Form submit 自体は write でなく、原 payload の明示確認を準備する。 */
  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    manager.choose(manager.editor.original ? 'edit' : 'create')
  }

  return <div className="projectManagement" data-project-management="" hidden={props.tab !== 'projects' && props.tab !== 'archived'}>
    {manager.saved && <p role="status">{messages.projectManagement.saved}</p>}
    {manager.previousUnknown && <details className="panel projectPreviousUnknown"><summary>{messages.projectManagement.acknowledgedHint}</summary>
      {manager.previousUnknown.original && <ProjectFacts project={manager.previousUnknown.original} />}
      <ProjectDraftFacts draft={manager.previousUnknown.draft} action={manager.previousUnknown.action} />
    </details>}
    {manager.intent && <>
      <ProjectResponseNotice failure={manager.failure} />
      {manager.phase === 'confirm'
        ? <ProjectIntentConfirmation intent={manager.intent} busy={manager.mutation.busy} onConfirm={manager.confirm} onCancel={() => { manager.cancel(); focusLanding() }} />
        : <ProjectManagementReview key={manager.phase} intent={manager.intent} unknown={manager.phase === 'unknown'} onSessionEnded={props.onSessionEnded}
          onAdopt={manager.adopt} onAcknowledge={() => { manager.acknowledge(); list.refresh(); focusLanding() }} onCancel={() => { manager.cancel(); focusLanding() }} />}
    </>}
    <section className="projectManagementLayout tabPanel" id="project-panel-projects" aria-labelledby="project-tab-projects" role="tabpanel" hidden={props.tab !== 'projects'}>
      <section className="panel" aria-busy={list.pending}>
        <div className="panelHeader"><h2>{messages.projects.accessible}</h2><button className="secondaryButton" type="button" data-project-list-refresh="active" disabled={list.pending} onClick={list.refresh}>{messages.account.refresh}</button></div>
        {list.pending && <LoadingSkeleton label={messages.projects.loadingList} rows={2} />}
        <ProjectResponseNotice failure={list.failure} />
        {ready && !active.length && <EmptyState text={messages.projects.emptyActive} />}
        <ProjectRows projects={active} projectId={props.projectId} locked={!ready || manager.locked} admin onSelect={props.setProjectId} onAction={action} />
      </section>
      <aside className="panel projectSidePanel">
        <h2 ref={formHeading} tabIndex={-1}>{manager.editor.original ? messages.projects.editTitle : messages.projects.createTitle}</h2>
        {manager.editor.original && <ProjectFacts project={manager.editor.original} />}
        <ProjectMetadataForm draft={manager.editor.draft} editing={!!manager.editor.original} locked={manager.locked}
          onChange={manager.change} onSubmit={submit} onReset={() => { manager.edit(null); focusLanding() }} />
        <ProjectLinks />
      </aside>
    </section>
    <section className="panel archivedProjects tabPanel" id="project-panel-archived" aria-labelledby="project-tab-archived" role="tabpanel" hidden={props.tab !== 'archived'} aria-busy={list.pending}>
      <div className="panelHeader"><h2 ref={archivedHeading} tabIndex={-1}>{messages.projects.archivedTitle}</h2><button className="secondaryButton" type="button" data-project-list-refresh="archived" disabled={list.pending} onClick={list.refresh}>{messages.account.refresh}</button></div>
      <p className="hint">{messages.projects.archivedHint}</p>
      {list.pending && <LoadingSkeleton label={messages.projects.loadingArchived} rows={1} />}
      <ProjectResponseNotice failure={list.failure} />
      {ready && !archived.length && <EmptyState text={messages.projects.archivedEmpty} />}
      <ProjectRows projects={archived} projectId={props.projectId} locked={!ready || manager.locked} admin onSelect={props.setProjectId} onAction={action} />
    </section>
  </div>
}

/** 行は metadata と lifecycle action の入口だけを担い、独立 mutation を持たない。 */
export function ProjectRows({ projects, projectId, locked, admin, onSelect, onAction }: {
  projects: ProjectRecord[]; projectId: string; locked: boolean; admin: boolean
  onSelect: (id: string) => void
  onAction: (action: Exclude<ProjectIntent['action'], 'create'>, project: ProjectRecord) => void
}) {
  const messages = useMessages()
  return <div className="projectList">{projects.map((project) => <article className={`projectItem${project.project_id.toLowerCase() === projectId.toLowerCase() ? ' projectItemSelected' : ''}`} data-project-row={project.project_id} key={project.project_id}>
    <button className="projectSelect" type="button" data-project-action="select" onClick={() => onSelect(project.project_id)}>
      <strong>{project.name}</strong><span>{project.key}</span><small>{project.description || messages.projects.noDescription}</small>
      <small>{messages.projectManagement.states[project.status]}</small>
    </button>
    {admin && <div className="projectItemActions"><button className="secondaryButton" type="button" data-project-action="edit" disabled={locked} onClick={() => onAction('edit', project)}>{messages.projects.edit}</button>
      {project.status === 'ACTIVE'
        ? <button className="dangerButton" type="button" data-project-action="archive" disabled={locked} onClick={() => onAction('archive', project)}>{messages.projects.archive}</button>
        : <><button className="secondaryButton" type="button" data-project-action="restore" disabled={locked} onClick={() => onAction('restore', project)}>{messages.projects.restore}</button>
          <button className="dangerButton" type="button" data-project-action="delete" disabled={locked} onClick={() => onAction('delete', project)}>{messages.projects.deleteProject}</button></>}
    </div>}
    <details className="detailDisclosure projectRowDetails"><summary>{messages.elements.technicalDetails}</summary>
      <p className="mono">{project.project_id}</p><p>{messages.projectManagement.version}: {project.row_version}</p>
    </details>
  </article>)}</div>
}

/** Settings を再構成しない controlled form。送信後は原草稿を編集不能にする。 */
export function ProjectMetadataForm({ draft, editing, locked, onChange, onSubmit, onReset }: {
  draft: ProjectDraft; editing: boolean; locked: boolean; onChange: (draft: ProjectDraft) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void; onReset: () => void
}) {
  const messages = useMessages()
  return <form data-project-form="" onSubmit={onSubmit}><fieldset disabled={locked}>
    {!editing && <label>{messages.projects.keyLabel}<input name="key" maxLength={100} pattern="[a-z0-9][a-z0-9-]*" value={draft.key} required onChange={(event) => onChange({ ...draft, key: event.target.value })} /></label>}
    <label>{messages.projects.nameLabel}<input name="name" maxLength={200} value={draft.name} required onChange={(event) => onChange({ ...draft, name: event.target.value })} /></label>
    <label>{messages.projects.descriptionLabel}<textarea className="compactTextarea" name="description" maxLength={4000} value={draft.description} onChange={(event) => onChange({ ...draft, description: event.target.value })} /></label>
    <label>{messages.projects.retentionLabel}<input name="retention_days" type="number" min={1} max={3650} value={draft.retentionDays} required onChange={(event) => onChange({ ...draft, retentionDays: event.target.value })} /></label>
    <p className="hint">{messages.projectManagement.settingsPreserved}</p>
    <div className="projectFormActions"><button className="primaryButton" type="submit" disabled={!validProjectDraft(draft, !editing)}>{editing ? messages.projects.saveChanges : messages.projects.createSubmit}</button>
      {editing && <button className="secondaryButton" type="button" onClick={onReset}>{messages.projects.cancelEdit}</button>}</div>
  </fieldset></form>
}

/** Platform の既存移動先を保ち、管理草稿を URL へ載せない。 */
function ProjectLinks() {
  const messages = useMessages()
  return <div className="inlineActions"><a href={routeHref('skills')}>{messages.projects.goSkills}</a><a href={routeHref('workspace')}>{messages.projects.goWorkspace}</a></div>
}
