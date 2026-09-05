import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'

import type { ProjectState } from '../appState'
import type { AuthSessionRecord } from '../api'
import { EmptyState, LoadingSkeleton, PageHeader, useConfirmDialog } from '../components/PageElements'
import { useMessages } from '../i18n'
import type { UiMessages } from '../lib/i18n/messages'
import { routeHref } from '../lib/routing'
import {
  ApiProblemError,
  archiveProject,
  createProject,
  createProjectModule,
  deleteProject,
  deleteProjectModule,
  loadProjectModules,
  loadProjectTasks,
  loadProjects,
  unarchiveProject,
  updateProject,
  updateProjectModule,
  type ProjectModuleRecord,
  type ProjectRecord,
} from '../api'

/** Module 一覧取得の非同期状態。 */
type ModulesState =
  | { status: 'loading' }
  | { status: 'ready'; modules: ProjectModuleRecord[] }
  | { status: 'error'; message: string }

/** 項目管理画面の 3 区分。同時に一つだけ見せ、縦積みを解消する。 */
type ProjectsPageTab = 'projects' | 'archived' | 'modules'

/** アーカイブ済み Project 一覧の非同期状態。 */
type ArchivedProjectsState =
  | { status: 'loading' }
  | { status: 'ready'; projects: ProjectRecord[] }
  | { status: 'error'; message: string }

/** Project 削除拒否の Problem code を利用者語へ変換する。
 *
 * backend は Problem contract に追加 field を持てないため、阻害理由を code で区別している。
 * どちらの code でもない失敗は元の message をそのまま見せ、原因を握り潰さない。
 */
export function projectDeleteErrorMessage(error: unknown, messages: UiMessages): string {
  if (error instanceof ApiProblemError) {
    if (error.code === 'project_delete_blocked_by_runs') return messages.projects.deleteBlockedByRuns
    if (error.code === 'project_delete_requires_archive') return messages.projects.deleteNeedsArchive
  }
  return error instanceof Error ? error.message : messages.projects.deleteFailed
}

/** 束縛候補となる PUBLISHED SkillVersion の表示用選択肢。 */
interface SkillOption {
  skill_version_id: string
  label: string
}

/** 束縛済みだが候補一覧に無い version ID を返す。
 *
 * 候補は published task catalog 由来のため、廃止・Project 無効化された版は候補から消える。
 * 一方 module 編集の初期選択は既存束縛をそのまま載せるので、描画しないと checkbox が無く
 * 外せないまま送信され続け、保存が永久に失敗する。呼び出し側はこれを警告付きで描画する。
 */
export function staleBindingIds(
  selected: readonly string[],
  options: readonly SkillOption[],
): string[] {
  return selected.filter(
    (id) => !options.some((option) => option.skill_version_id === id),
  )
}

/** 現在の Project context を選択し、共有範囲と未提供機能を説明する画面。 */
export function ProjectsPage({
  projectId,
  setProjectId,
  session,
  projectState,
  onProjectChanged,
  onProjectArchived,
}: {
  projectId: string
  setProjectId: (projectId: string) => void
  session: AuthSessionRecord
  projectState: ProjectState
  onProjectChanged: (project: ProjectRecord) => void
  onProjectArchived: (project: ProjectRecord) => void
}) {
  const messages = useMessages()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 編集中の Project。null は「新規作成 form」を意味する。
  const [editing, setEditing] = useState<ProjectRecord | null>(null)
  // アーカイブ一覧は shell の共有 Project context とは別に読む。sidebar の選択肢へ
  // アーカイブ済みを混ぜると、実行できない Project を選べてしまうため分離する。
  const [archivedRevision, setArchivedRevision] = useState(0)
  const [pageTab, setPageTab] = useState<ProjectsPageTab>('projects')
  const { confirm, confirmDialog } = useConfirmDialog()
  const mounted = useRef(true)
  useEffect(() => () => { mounted.current = false }, [])

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    setBusy(true)
    setError(null)
    try {
      if (editing === null) {
        const created = await createProject({
          key: String(form.get('key') ?? ''),
          name: String(form.get('name') ?? ''),
          description: String(form.get('description') ?? ''),
          retention_days: Number(form.get('retention_days') ?? 90),
          settings: {},
        }, session.csrf_token)
        if (!mounted.current) return
        onProjectChanged(created)
        setProjectId(created.project_id)
        formElement.reset()
      } else {
        // key と status は更新対象外。API も受け付けないため form から外してある。
        const updated = await updateProject(editing.project_id, {
          name: String(form.get('name') ?? ''),
          description: String(form.get('description') ?? ''),
          retention_days: Number(form.get('retention_days') ?? 90),
        }, session.csrf_token)
        if (!mounted.current) return
        onProjectChanged(updated)
        setEditing(null)
      }
    } catch (reason) {
      if (mounted.current) {
        setError(reason instanceof Error
          ? reason.message
          : editing === null ? messages.projects.createFailed : messages.projects.updateFailed)
      }
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const archive = async (project: ProjectRecord): Promise<void> => {
    if (!await confirm({
      title: messages.projects.archive,
      message: messages.projects.archiveConfirm(project.name),
      confirmLabel: messages.projects.archive,
    })) return
    setBusy(true)
    setError(null)
    try {
      const archived = await archiveProject(project.project_id, session.csrf_token)
      if (!mounted.current) return
      onProjectArchived(archived)
      if (editing?.project_id === project.project_id) setEditing(null)
      setArchivedRevision((current) => current + 1)
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : messages.projects.archiveFailed)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const projects = projectState.status === 'ready' ? projectState.projects : []
  return (
    <>
      <PageHeader
        title={messages.routes.projects.label}
        description={messages.projects.description}
      />
      {/* 項目・アーカイブ・模块を tab で分け、1 画面 3 段の縦積みを解消する。
          非活性側も hidden で DOM に残す(頁面測試の toContain と入力途中の保持のため)。 */}
      <div className="tabBar" role="tablist" aria-label={messages.projects.pageTabsAria}>
        <ProjectsTabButton current={pageTab} tab="projects" onSelect={setPageTab}>
          {messages.projects.tabProjects}
          <span className="eventCount">{projects.length}</span>
        </ProjectsTabButton>
        {session.user.system_role === 'ADMIN' && (
          <ProjectsTabButton current={pageTab} tab="archived" onSelect={setPageTab}>
            {messages.projects.tabArchived}
          </ProjectsTabButton>
        )}
        <ProjectsTabButton current={pageTab} tab="modules" onSelect={setPageTab}>
          {messages.projects.tabModules}
        </ProjectsTabButton>
      </div>
      <section className="projectManagementLayout tabPanel" role="tabpanel" hidden={pageTab !== 'projects'}>
        <section className="panel">
          <div className="panelHeader"><h2>{messages.projects.accessible}</h2><span className="eventCount">{projects.length}</span></div>
          {projectState.status === 'loading' && <LoadingSkeleton label={messages.projects.loadingList} rows={3} />}
          {projectState.status === 'error' && <p className="error" role="alert">{projectState.message}</p>}
          {projectState.status === 'ready' && projects.length === 0 && (
            <div className="emptyState"><p>{messages.projects.emptyActive}</p></div>
          )}
          <div className="projectList">
            {projects.map((project) => (
              <article className={project.project_id === projectId ? 'projectItem projectItemSelected' : 'projectItem'} key={project.project_id}>
                <button className="projectSelect" onClick={() => setProjectId(project.project_id)} type="button">
                  <strong>{project.name}</strong><span>{project.key}</span><small>{project.description || messages.projects.noDescription}</small>
                </button>
                {session.user.system_role === 'ADMIN' && (
                  <div className="projectItemActions">
                    <button className="secondaryButton" disabled={busy} onClick={() => { setEditing(project); setError(null) }} type="button">{messages.projects.edit}</button>
                    <button className="dangerButton" disabled={busy} onClick={() => void archive(project)} type="button">{messages.projects.archive}</button>
                  </div>
                )}
              </article>
            ))}
          </div>
        </section>
        <aside className="panel projectSidePanel">
          {session.user.system_role === 'ADMIN' ? (
            // key は identity のため編集させない。form の再構築を確実にするため key prop で
            // 編集対象を切り替え、defaultValue が前の Project の値を持ち越さないようにする。
            <form key={editing?.project_id ?? 'create'} onSubmit={(event) => void submit(event)}>
              <div className="panelHeader"><h2>{editing === null ? messages.projects.createTitle : messages.projects.editTitle}</h2></div>
              {editing === null
                ? <label>{messages.projects.keyLabel}<input name="key" pattern="[a-z0-9][a-z0-9-]*" required /></label>
                : <div className="sourceField"><span className="sourceFieldLabel">{messages.projects.keyLabel}</span><span className="sourceFieldStatic mono">{editing.key}</span></div>}
              <label>{messages.projects.nameLabel}<input defaultValue={editing?.name ?? ''} maxLength={200} name="name" required /></label>
              <label>{messages.projects.descriptionLabel}<textarea className="compactTextarea" defaultValue={editing?.description ?? ''} maxLength={4000} name="description" /></label>
              <label>{messages.projects.retentionLabel}<input defaultValue={editing === null ? '90' : String(editing.retention_days)} max="3650" min="1" name="retention_days" required type="number" /></label>
              {error && <p className="error" role="alert">{error}</p>}
              <div className="projectFormActions">
                <button className="primaryButton" disabled={busy} type="submit">{busy ? messages.elements.processing : editing === null ? messages.projects.createSubmit : messages.projects.saveChanges}</button>
                {editing !== null && (
                  <button className="secondaryButton" disabled={busy} onClick={() => { setEditing(null); setError(null) }} type="button">{messages.projects.cancelEdit}</button>
                )}
              </div>
            </form>
          ) : (
            <div><div className="panelHeader"><h2>{messages.projects.permissionsTitle}</h2></div><p className="hint">{messages.projects.permissionsHint}</p></div>
          )}
          <div className="inlineActions">
            <a href={routeHref('skills')}>{messages.projects.goSkills}</a>
            <a href={routeHref('workspace')}>{messages.projects.goWorkspace}</a>
          </div>
        </aside>
      </section>
      {session.user.system_role === 'ADMIN' && (
        <div className="tabPanel" role="tabpanel" hidden={pageTab !== 'archived'}>
          <ArchivedProjectsSection
            onRestored={(project) => { onProjectChanged(project); setArchivedRevision((current) => current + 1) }}
            revision={archivedRevision}
            session={session}
          />
        </div>
      )}
      <div className="tabPanel" role="tabpanel" hidden={pageTab !== 'modules'}>
        {projectId
          ? <ModulesSection projectId={projectId} session={session} />
          : <EmptyState text={messages.projects.modulesNeedProject} />}
      </div>
      {confirmDialog}
    </>
  )
}

/** 項目管理画面の区分 tab button。選択状態を aria-selected で表す。 */
function ProjectsTabButton({ current, tab, onSelect, children }: {
  current: ProjectsPageTab
  tab: ProjectsPageTab
  onSelect: (tab: ProjectsPageTab) => void
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

/** アーカイブ済み Project を可視化し、復元と key 解放のための削除を提供する区画。
 *
 * Archive は key の一意制約を解かないため、一覧から消えたまま同じ key で作り直せない状態に
 * なりやすい。ここで「まだ存在すること」を見せ、復元と削除のどちらでも詰まりを解けるようにする。
 */
function ArchivedProjectsSection({ session, revision, onRestored }: {
  session: AuthSessionRecord
  revision: number
  onRestored: (project: ProjectRecord) => void
}) {
  const messages = useMessages()
  const [state, setState] = useState<ArchivedProjectsState>({ status: 'loading' })
  const [busyProjectId, setBusyProjectId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [localRevision, setLocalRevision] = useState(0)
  const { confirm, confirmDialog } = useConfirmDialog()
  const loadController = useRef<AbortController | null>(null)
  const mutateController = useRef<AbortController | null>(null)

  useEffect(() => () => {
    loadController.current?.abort()
    mutateController.current?.abort()
  }, [])

  useEffect(() => {
    loadController.current?.abort()
    const controller = new AbortController()
    loadController.current = controller
    setState((current) => current.status === 'ready' ? current : { status: 'loading' })
    void loadProjects(true, controller.signal)
      .then((projects) => setState({
        status: 'ready',
        projects: projects.filter((project) => project.status === 'ARCHIVED'),
      }))
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) {
          setState({
            status: 'error',
            message: caught instanceof Error ? caught.message : messages.projects.loadArchivedFailed,
          })
        }
      })
    return () => controller.abort()
  }, [revision, localRevision])

  async function restore(project: ProjectRecord): Promise<void> {
    mutateController.current?.abort()
    const controller = new AbortController()
    mutateController.current = controller
    setBusyProjectId(project.project_id)
    setError(null)
    try {
      const restored = await unarchiveProject(project.project_id, session.csrf_token, controller.signal)
      if (controller.signal.aborted) return
      onRestored(restored)
      setLocalRevision((current) => current + 1)
    } catch (caught) {
      if (!controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : messages.projects.restoreFailed)
      }
    } finally {
      if (!controller.signal.aborted) setBusyProjectId(null)
    }
  }

  async function remove(project: ProjectRecord): Promise<void> {
    if (!await confirm({
      title: messages.projects.deleteProject,
      message: messages.projects.deleteConfirm(project.name, project.key),
      confirmLabel: messages.projects.deleteProject,
      destructive: true,
    })) return
    mutateController.current?.abort()
    const controller = new AbortController()
    mutateController.current = controller
    setBusyProjectId(project.project_id)
    setError(null)
    try {
      await deleteProject(project.project_id, session.csrf_token, controller.signal)
      if (controller.signal.aborted) return
      setLocalRevision((current) => current + 1)
    } catch (caught) {
      if (!controller.signal.aborted) setError(projectDeleteErrorMessage(caught, messages))
    } finally {
      if (!controller.signal.aborted) setBusyProjectId(null)
    }
  }

  const projects = state.status === 'ready' ? state.projects : []
  return (
    <section className="panel archivedProjects" aria-label={messages.projects.archivedAria}>
      <div className="panelHeader">
        <h2>{messages.projects.archivedTitle}</h2>
        {state.status === 'ready' && <span className="eventCount">{projects.length}</span>}
      </div>
      <p className="hint">{messages.projects.archivedHint}</p>
      {state.status === 'loading' && <LoadingSkeleton label={messages.projects.loadingArchived} rows={1} />}
      {state.status === 'error' && <p className="error" role="alert">{state.message}</p>}
      {state.status === 'ready' && projects.length === 0 && (
        <EmptyState text={messages.projects.archivedEmpty} />
      )}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="projectList">
        {projects.map((project) => (
          <article className="projectItem projectItemArchived" key={project.project_id}>
            <div className="projectArchivedIdentity">
              <strong>{project.name}</strong>
              <span className="mono">{project.key}</span>
              <small>{project.description || messages.projects.noDescription}</small>
            </div>
            <div className="projectItemActions">
              <button className="secondaryButton" disabled={busyProjectId === project.project_id} onClick={() => void restore(project)} type="button">
                {busyProjectId === project.project_id ? messages.elements.processing : messages.projects.restore}
              </button>
              <button className="dangerButton" disabled={busyProjectId === project.project_id} onClick={() => void remove(project)} type="button">
                {messages.projects.deleteProject}
              </button>
            </div>
          </article>
        ))}
      </div>
      {confirmDialog}
    </section>
  )
}

/** 現在 Project の module(已発布 Skill の束)を一覧・作成・編集・削除する設定区画。 */
function ModulesSection({ projectId, session }: {
  projectId: string
  session: AuthSessionRecord
}) {
  const messages = useMessages()
  const isAdmin = session.user.system_role === 'ADMIN'
  const [modulesState, setModulesState] = useState<ModulesState>({ status: 'loading' })
  const [options, setOptions] = useState<SkillOption[]>([])
  const [editingId, setEditingId] = useState<string | null>(null)
  const { confirm, confirmDialog } = useConfirmDialog()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const loadController = useRef<AbortController | null>(null)
  const optionsController = useRef<AbortController | null>(null)
  const mutateController = useRef<AbortController | null>(null)

  useEffect(() => () => {
    loadController.current?.abort()
    optionsController.current?.abort()
    mutateController.current?.abort()
  }, [])

  useEffect(() => {
    loadController.current?.abort()
    const controller = new AbortController()
    loadController.current = controller
    setModulesState((current) => current.status === 'ready' ? current : { status: 'loading' })
    void loadProjectModules(projectId, controller.signal)
      .then((modules) => setModulesState({ status: 'ready', modules }))
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) {
          setModulesState({
            status: 'error',
            message: caught instanceof Error ? caught.message : 'Unknown module API error',
          })
        }
      })
    return () => controller.abort()
  }, [projectId, revision])

  // 束縛候補は published task catalog から version 単位に集約する(専用 endpoint を増やさない)。
  useEffect(() => {
    optionsController.current?.abort()
    const controller = new AbortController()
    optionsController.current = controller
    void loadProjectTasks(projectId, controller.signal)
      .then((catalog) => {
        const seen = new Map<string, SkillOption>()
        for (const task of catalog.tasks) {
          if (!seen.has(task.skill_version_id)) {
            seen.set(task.skill_version_id, {
              skill_version_id: task.skill_version_id,
              label: `${task.skill_name} v${task.version}`,
            })
          }
        }
        setOptions([...seen.values()])
      })
      .catch(() => {
        if (!controller.signal.aborted) setOptions([])
      })
    return () => controller.abort()
  }, [projectId, revision])

  /** Form を作成 mode(空)へ戻す。 */
  function resetForm(): void {
    setEditingId(null)
    setName('')
    setDescription('')
    setSelected([])
  }

  /** 既存 module を form へ読み込み、編集 mode に切り替える。 */
  function startEdit(module: ProjectModuleRecord): void {
    setEditingId(module.module_id)
    setName(module.name)
    setDescription(module.description)
    setSelected(module.skills.map((skill) => skill.skill_version_id))
    setError(null)
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    mutateController.current?.abort()
    const controller = new AbortController()
    mutateController.current = controller
    setBusy(true)
    setError(null)
    const input = { name, description, skill_version_ids: selected }
    try {
      if (editingId === null) {
        await createProjectModule(projectId, input, session.csrf_token, controller.signal)
      } else {
        await updateProjectModule(projectId, editingId, input, session.csrf_token, controller.signal)
      }
      if (controller.signal.aborted) return
      resetForm()
      setRevision((current) => current + 1)
    } catch (caught) {
      if (!controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : messages.projects.modules.saveFailed)
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  async function remove(module: ProjectModuleRecord): Promise<void> {
    if (!await confirm({
      title: messages.projects.modules.remove,
      message: messages.projects.modules.removeConfirm(module.name),
      confirmLabel: messages.projects.modules.remove,
      destructive: true,
    })) return
    mutateController.current?.abort()
    const controller = new AbortController()
    mutateController.current = controller
    setBusy(true)
    setError(null)
    try {
      await deleteProjectModule(projectId, module.module_id, session.csrf_token, controller.signal)
      if (controller.signal.aborted) return
      if (editingId === module.module_id) resetForm()
      setRevision((current) => current + 1)
    } catch (caught) {
      if (!controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : messages.projects.modules.removeFailed)
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  const modules = modulesState.status === 'ready' ? modulesState.modules : []
  // 束縛済み version の表示名。候補一覧から消えた版でも module 側の情報で名前を出す。
  const boundLabels = new Map(
    modules.flatMap((module) => module.skills.map(
      (skill) => [skill.skill_version_id, `${skill.skill_name} v${skill.version}`] as const,
    )),
  )
  const staleSelected = staleBindingIds(selected, options)
  return (
    <section className="panel modulesPanel" aria-label={messages.projects.modules.sectionAria}>
      <div className="panelHeader">
        <h2>{messages.projects.modules.title}</h2>
        {modulesState.status === 'ready' && <span className="eventCount">{modules.length}</span>}
      </div>
      <p className="hint">{messages.projects.modules.hint}</p>
      <div className="modulesLayout">
        <div className="moduleList">
          {modulesState.status === 'loading' && <LoadingSkeleton label={messages.projects.modules.loading} rows={2} />}
          {modulesState.status === 'error' && <p className="error" role="alert">{modulesState.message}</p>}
          {modulesState.status === 'ready' && modules.length === 0 && (
            <EmptyState text={messages.projects.modules.empty} />
          )}
          {modules.map((module) => (
            <article className={`moduleItem${editingId === module.module_id ? ' moduleItemEditing' : ''}`} key={module.module_id}>
              <div className="moduleItemBody">
                <strong>{module.name}</strong>
                {module.description && <p>{module.description}</p>}
                <div className="moduleSkillChips">
                  {module.skills.map((skill) => (
                    <code key={skill.skill_version_id}>{skill.skill_name} v{skill.version}</code>
                  ))}
                </div>
              </div>
              {isAdmin && (
                <div className="moduleItemActions">
                  <button className="secondaryButton" disabled={busy} type="button" onClick={() => startEdit(module)}>{messages.projects.modules.edit}</button>
                  <button className="dangerButton" disabled={busy} type="button" onClick={() => void remove(module)}>{messages.projects.modules.remove}</button>
                </div>
              )}
            </article>
          ))}
        </div>
        {isAdmin ? (
          <form className="moduleForm" onSubmit={(event) => void submit(event)}>
            <h3>{editingId === null ? messages.projects.modules.createTitle : messages.projects.modules.editTitle}</h3>
            <label>{messages.projects.modules.nameLabel}<input maxLength={200} required value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label>{messages.projects.modules.descriptionLabel}<textarea className="compactTextarea" maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
            <fieldset className="moduleSkillPicker">
              <legend>{messages.projects.modules.pickerLegend}</legend>
              {options.length === 0 && (
                <p className="hint">{messages.projects.modules.noPublished}</p>
              )}
              {options.map((option) => (
                <label className="moduleSkillOption" key={option.skill_version_id}>
                  <input
                    checked={selected.includes(option.skill_version_id)}
                    type="checkbox"
                    onChange={(event) => setSelected((current) => event.target.checked
                      ? [...current, option.skill_version_id]
                      : current.filter((id) => id !== option.skill_version_id))}
                  />
                  <span>{option.label}</span>
                </label>
              ))}
              {staleSelected.map((versionId) => (
                <label className="moduleSkillOption moduleSkillOptionStale" key={versionId}>
                  <input
                    checked
                    type="checkbox"
                    onChange={() => setSelected((current) => current.filter((id) => id !== versionId))}
                  />
                  <span>
                    {boundLabels.get(versionId) ?? versionId}
                    <small>{messages.projects.modules.staleBinding}</small>
                  </span>
                </label>
              ))}
            </fieldset>
            {error && <p className="error" role="alert">{error}</p>}
            <div className="moduleFormActions">
              <button className="primaryButton" disabled={busy || selected.length === 0 || !name.trim()} type="submit">
                {busy ? messages.elements.processing : editingId === null ? messages.projects.modules.createTitle : messages.projects.modules.saveChanges}
              </button>
              {editingId !== null && (
                <button className="secondaryButton" disabled={busy} type="button" onClick={resetForm}>{messages.projects.modules.cancelEdit}</button>
              )}
            </div>
          </form>
        ) : (
          <div className="moduleForm">
            <h3>{messages.projects.modules.configTitle}</h3>
            <p className="hint">{messages.projects.modules.configHint}</p>
          </div>
        )}
      </div>
      {confirmDialog}
    </section>
  )
}
