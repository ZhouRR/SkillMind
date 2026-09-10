import { useEffect, useRef, useState, type FormEvent } from 'react'

import { createProjectModule, deleteProjectModule, loadProjectModules, loadProjectTasks, updateProjectModule, type AuthSessionRecord, type ProjectModuleRecord } from '../api'
import { EmptyState, LoadingSkeleton, useConfirmDialog } from './PageElements'
import { useMessages } from '../i18n'
import { apiErrorMessage } from '../lib/apiFeedback'

/** Module 一覧取得の非同期状態。 */
type ModulesState =
  | { status: 'loading' }
  | { status: 'ready'; modules: ProjectModuleRecord[] }
  | { status: 'error'; message: string }

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

/** 現在 Project の module(已発布 Skill の束)を一覧・作成・編集・削除する設定区画。 */
export function ProjectModulesPanel({ projectId, session }: {
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
            message: apiErrorMessage(caught, 'Unknown module API error', messages),
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
        setError(apiErrorMessage(caught, messages.projects.modules.saveFailed, messages))
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
        setError(apiErrorMessage(caught, messages.projects.modules.removeFailed, messages))
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
