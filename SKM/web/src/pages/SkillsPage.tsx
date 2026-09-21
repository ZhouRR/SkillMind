import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from 'react'

import {
  ApiProblemError,
  createSkillVersionDraft,
  deleteSkillVersion,
  deprecateSkillVersion,
  disableProjectSkillVersion,
  enableProjectSkillVersion,
  listProjectSkillVersions,
  listSkillVersions,
  parseSkillSource,
  publishSkillVersion,
  saveSkillImport,
  uploadSkillFiles,
  type SkillParseResult,
  type SkillSourceFile,
  type SkillVersionRecord,
  type StoredSkillPreviewRecord,
} from '../api'
import { EmptyState, PageHeader, useConfirmDialog } from '../components/PageElements'
import { useMessages } from '../i18n'
import { useSkillInterpretation } from '../hooks/useSkillInterpretation'
import { apiErrorMessage } from '../lib/apiFeedback'
import { readUploadedSourcePreview, type UploadedSourceFile } from '../lib/skillUpload'
import { SkillLibraryPanel, SkillVersionDetail, type SkillLibraryState, type ProjectEnablementState } from '../components/SkillLibraryPanel'
import { SavedSkillIdentity, SkillParseSummary, UploadedSourceFiles } from '../components/SkillSourcePreview'
import { InterpretationExecutionView, InterpretStreamView } from '../components/SkillInterpretationPreview'
import { SkillTabButton } from '../components/SkillTabButton'

/** Skill parser panel の非同期状態。 */
type SkillParseState =
  | { status: 'idle' }
  | { status: 'parsing' }
  | { status: 'ready'; result: SkillParseResult }
  | { status: 'error'; message: string }

/** SkillSource/Interpretation 永続化の非同期状態。 */
type SkillSaveState =
  | { status: 'idle' }
  | { status: 'saving' }
  | { status: 'ready'; stored: StoredSkillPreviewRecord }
  | { status: 'error'; message: string }

/** SkillVersion DRAFT 作成・publish の非同期状態。 */
type SkillVersionState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; version: SkillVersionRecord }
  | { status: 'error'; message: string }

/** 画面の 2 大区分。取込〜発行の作業台と、組織 library の管理を同時に一つだけ見せる。 */
type SkillsPageTab = 'workbench' | 'library'

/** Organization Skill library の解析、model 解釈、修訂と発行を担当する画面。 */
export function SkillsPage({ projectId, csrfToken }: {
  projectId: string
  csrfToken: string
}) {
  const messages = useMessages()
  const [skillMarkdown, setSkillMarkdown] = useState('')
  const [referenceMarkdown, setReferenceMarkdown] = useState('')
  const [uploadedSource, setUploadedSource] = useState<UploadedSourceFile[] | null>(null)
  const [parseState, setParseState] = useState<SkillParseState>({ status: 'idle' })
  const [saveState, setSaveState] = useState<SkillSaveState>({ status: 'idle' })
  const [versionState, setVersionState] = useState<SkillVersionState>({ status: 'idle' })
  const [libraryState, setLibraryState] = useState<SkillLibraryState>({ status: 'loading' })
  const [enablementState, setEnablementState] = useState<ProjectEnablementState>({ status: 'idle' })
  const [libraryActionError, setLibraryActionError] = useState<{ versionId: string; message: string } | null>(null)
  const [libraryBusyVersionId, setLibraryBusyVersionId] = useState<string | null>(null)
  const [pageTab, setPageTab] = useState<SkillsPageTab>('library')
  const { confirm, confirmDialog } = useConfirmDialog()
  const parseController = useRef<AbortController | null>(null)
  const saveController = useRef<AbortController | null>(null)
  const uploadController = useRef<AbortController | null>(null)
  const versionController = useRef<AbortController | null>(null)
  const libraryController = useRef<AbortController | null>(null)
  const enablementController = useRef<AbortController | null>(null)
  const libraryMutationController = useRef<AbortController | null>(null)
  const { interpretState, adjustState, instruction, setInstruction, pendingRequestId,
    resetInterpretation, confirmPendingInterpretation, dismissInterpretation, handleInterpret,
    handleAdjust, acknowledgeDraft } = useSkillInterpretation(csrfToken,
      () => { versionController.current?.abort(); setVersionState({ status: 'idle' }) }, () => setPageTab('workbench'))

  // DOM が切り替わった commit で旧要求を閉じ、passive cleanup 前の成功も破棄する。
  useLayoutEffect(() => () => {
    parseController.current?.abort()
    saveController.current?.abort()
    uploadController.current?.abort()
    versionController.current?.abort()
    libraryController.current?.abort()
    enablementController.current?.abort()
    libraryMutationController.current?.abort()
  }, [])

  useEffect(() => {
    void refreshLibrary()
    return () => libraryController.current?.abort()
  }, [])

  useEffect(() => {
    void refreshEnablements()
    return () => enablementController.current?.abort()
  }, [projectId])

  /** Organization の version 一覧を再取得し、古い非同期応答を state へ入れない。 */
  async function refreshLibrary(): Promise<void> {
    libraryController.current?.abort()
    const controller = new AbortController()
    libraryController.current = controller
    setLibraryState((current) => ({ status: 'loading', versions: current.versions }))
    try {
      const versions = await listSkillVersions(controller.signal)
      if (!controller.signal.aborted) setLibraryState({ status: 'ready', versions })
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setLibraryState((current) => ({
          status: 'error', versions: current.versions,
          message: apiErrorMessage(error, messages.skills.loadLibraryFailed, messages),
        }))
      }
    }
  }

  /** 選択 Project の active/disabled enablement を監査表示用に再取得する。 */
  async function refreshEnablements(): Promise<void> {
    enablementController.current?.abort()
    if (!projectId) {
      setEnablementState({ status: 'idle' })
      return
    }
    const controller = new AbortController()
    enablementController.current = controller
    setEnablementState({ status: 'loading' })
    try {
      const enablements = await listProjectSkillVersions(projectId, true, controller.signal)
      if (!controller.signal.aborted) setEnablementState({ status: 'ready', enablements })
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setEnablementState({
          status: 'error',
          message: apiErrorMessage(error, messages.skills.loadEnablementsFailed, messages),
        })
      }
    }
  }

  /** Editor の現在値から backend contract の file collection を作る。 */
  function currentFiles(): SkillSourceFile[] {
    return [
      { path: 'SKILL.md', content: skillMarkdown },
      ...(referenceMarkdown.trim() ? [{ path: 'references/rules.md', content: referenceMarkdown }] : []),
    ]
  }

  /** 原文を替えたときだけ、確認済みの下流表示を無効にする。未決 UUID は hook が保持する。 */
  function resetDownstream(): void {
    resetInterpretation()
    versionController.current?.abort()
    setVersionState({ status: 'idle' })
  }

  /** Model を呼ばない parser preview を実行し、以前の保存結果を無効化する。 */
  async function handleParse(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    parseController.current?.abort()
    const controller = new AbortController()
    parseController.current = controller
    setParseState({ status: 'parsing' })
    setSaveState({ status: 'idle' })
    resetDownstream()
    try {
      const result = await parseSkillSource(currentFiles(), csrfToken, controller.signal)
      if (parseController.current !== controller || controller.signal.aborted) return
      setParseState({ status: 'ready', result })
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setParseState({ status: 'error', message: apiErrorMessage(error, 'Unknown parser error', messages) })
      }
    }
  }

  /** Preview と同じ source を不可変な SkillSource/Interpretation として保存する。 */
  async function handleSave(): Promise<void> {
    saveController.current?.abort()
    const controller = new AbortController()
    saveController.current = controller
    setSaveState({ status: 'saving' })
    resetDownstream()
    try {
      const stored = await saveSkillImport(currentFiles(), csrfToken, controller.signal)
      if (saveController.current !== controller || controller.signal.aborted) return
      setSaveState({ status: 'ready', stored })
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setSaveState({ status: 'error', message: apiErrorMessage(error, 'Unknown persistence error', messages) })
      }
    }
  }

  /** 選択した目录（binary asset 可）を multipart upload し、保存済み source として扱う。 */
  async function handleUpload(files: File[]): Promise<void> {
    if (files.length === 0) return
    uploadController.current?.abort()
    const controller = new AbortController()
    uploadController.current = controller
    setParseState({ status: 'idle' })
    setSaveState({ status: 'saving' })
    resetDownstream()
    try {
      // Preview は手元 File から読み、保存成功と同時に「実際に保存された源文件」として左欄へ出す。
      const [preview, stored] = await Promise.all([
        readUploadedSourcePreview(files),
        uploadSkillFiles(files, csrfToken, controller.signal),
      ])
      if (controller.signal.aborted) return
      setUploadedSource(preview)
      setSaveState({ status: 'ready', stored })
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setSaveState({ status: 'error', message: apiErrorMessage(error, 'Unknown upload error', messages) })
      }
    }
  }

  /** 指定 Interpretation を gate report 付き frozen DRAFT へ変換する。 */
  async function handleCreateDraft(interpretationId: string): Promise<void> {
    versionController.current?.abort()
    const controller = new AbortController()
    versionController.current = controller
    setVersionState({ status: 'loading' })
    try {
      const version = await createSkillVersionDraft(
        interpretationId,
        csrfToken,
        controller.signal,
      )
      if (versionController.current !== controller || controller.signal.aborted) return
      // 同じ解釈が下書きになった場合だけ片付け、新しい調整要求の UUID は消さない。
      acknowledgeDraft(interpretationId)
      setVersionState({ status: 'ready', version })
      await refreshLibrary()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setVersionState({ status: 'error', message: apiErrorMessage(error, 'Unknown draft API error', messages) })
      }
    }
  }

  /** Hard gate 通過済み DRAFT だけを publish API へ送る。 */
  async function handlePublish(version: SkillVersionRecord, fromLibrary = false): Promise<void> {
    if (!version.gate_passed || version.status !== 'DRAFT' || libraryBusyVersionId !== null) return
    const controllerRef = fromLibrary ? libraryMutationController : versionController
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    if (fromLibrary) {
      setLibraryActionError(null)
      setLibraryBusyVersionId(version.skill_version_id)
    } else setVersionState({ status: 'loading' })
    try {
      const warnings = version.gate_findings
        .filter((finding) => finding.severity === 'warning')
        .map((finding) => finding.code)
      if (warnings.length > 0 && !await confirm({
        title: messages.skills.publishVersion,
        message: messages.skills.publishWarningsConfirm(warnings),
        confirmLabel: messages.skills.publishVersion,
      })) {
        if (!fromLibrary && !controller.signal.aborted) setVersionState({ status: 'ready', version })
        return
      }
      if (controller.signal.aborted) return
      const published = await publishSkillVersion(
        version.skill_version_id,
        warnings,
        csrfToken,
        controller.signal,
      )
      if (controller.signal.aborted) return
      // 一覧からの発行は別の取込草稿を置き換えず、同じ版を開いている場合だけ同期する。
      setVersionState((current) => !fromLibrary || (current.status === 'ready'
        && current.version.skill_version_id === published.skill_version_id)
        ? { status: 'ready', version: published } : current)
      await refreshLibrary()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        const message = apiErrorMessage(error, 'Unknown publish API error', messages)
        if (fromLibrary) setLibraryActionError({ versionId: version.skill_version_id, message })
        else setVersionState({ status: 'error', message })
      }
    } finally {
      if (fromLibrary && !controller.signal.aborted) setLibraryBusyVersionId(null)
    }
  }

  /** Organization library の PUBLISHED 精確版を廃止する。 */
  async function handleDeprecate(version: SkillVersionRecord): Promise<void> {
    if (version.status !== 'PUBLISHED') return
    if (!await confirm({
      title: messages.skills.deprecateVersion,
      message: messages.skills.deprecateConfirm(version.name, version.version),
      confirmLabel: messages.skills.deprecateVersion,
    })) return
    libraryMutationController.current?.abort()
    const controller = new AbortController()
    libraryMutationController.current = controller
    setLibraryActionError(null)
    setLibraryBusyVersionId(version.skill_version_id)
    try {
      await deprecateSkillVersion(version.skill_version_id, csrfToken, controller.signal)
      await refreshLibrary()
      await refreshEnablements()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setLibraryActionError({
          versionId: version.skill_version_id,
          message: apiErrorMessage(error, messages.skills.deprecateFailed, messages),
        })
      }
    } finally {
      if (!controller.signal.aborted) setLibraryBusyVersionId(null)
    }
  }

  /** 監査参照のない DRAFT / DEPRECATED 版を organization library から取り除く。
   *
   * Run snapshot や composition から参照されている版は backend が 409 で拒否する。
   * 拒否理由は対象行へ出し、読取済みの一覧を操作エラーで置き換えない。
   */
  async function handleDelete(version: SkillVersionRecord): Promise<void> {
    if (version.status !== 'DRAFT' && version.status !== 'DEPRECATED') return
    if (!await confirm({
      title: messages.skills.deleteVersion,
      message: messages.skills.deleteConfirm(version.name, version.version),
      confirmLabel: messages.skills.deleteVersion,
      destructive: true,
    })) return
    libraryMutationController.current?.abort()
    const controller = new AbortController()
    libraryMutationController.current = controller
    setLibraryActionError(null)
    setLibraryBusyVersionId(version.skill_version_id)
    try {
      await deleteSkillVersion(version.skill_version_id, csrfToken, controller.signal)
      await refreshLibrary()
      await refreshEnablements()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setLibraryActionError({
          versionId: version.skill_version_id,
          message: error instanceof ApiProblemError && error.code === 'skill_version_delete_blocked'
            ? messages.skills.deleteBlocked
            : apiErrorMessage(error, messages.skills.deleteFailed, messages),
        })
      }
    } finally {
      if (!controller.signal.aborted) setLibraryBusyVersionId(null)
    }
  }

  /** Organization の PUBLISHED 精確版を現在 Project へ明示有効化する。 */
  async function handleEnable(version: SkillVersionRecord): Promise<void> {
    if (!projectId || version.status !== 'PUBLISHED') return
    libraryMutationController.current?.abort()
    const controller = new AbortController()
    libraryMutationController.current = controller
    setLibraryActionError(null)
    setLibraryBusyVersionId(version.skill_version_id)
    try {
      await enableProjectSkillVersion(
        projectId,
        version.skill_version_id,
        csrfToken,
        controller.signal,
      )
      await refreshEnablements()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setEnablementState({
          status: 'error',
          message: apiErrorMessage(error, messages.skills.enableFailed, messages),
        })
      }
    } finally {
      if (!controller.signal.aborted) setLibraryBusyVersionId(null)
    }
  }

  /** 現在 Project の有効化を監査行を残したまま停用する。 */
  async function handleDisable(version: SkillVersionRecord): Promise<void> {
    if (!projectId) return
    libraryMutationController.current?.abort()
    const controller = new AbortController()
    libraryMutationController.current = controller
    setLibraryActionError(null)
    setLibraryBusyVersionId(version.skill_version_id)
    try {
      await disableProjectSkillVersion(
        projectId,
        version.skill_version_id,
        csrfToken,
        controller.signal,
      )
      await refreshEnablements()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setEnablementState({
          status: 'error',
          message: apiErrorMessage(error, messages.skills.disableFailed, messages),
        })
      }
    } finally {
      if (!controller.signal.aborted) setLibraryBusyVersionId(null)
    }
  }

  const importFailure = [parseState, saveState, interpretState, adjustState, versionState]
    .find((state) => state.status === 'error')
  const importPending = parseState.status === 'parsing' ? messages.skills.parsing
    : saveState.status === 'saving' ? messages.skills.saving
      : interpretState.status === 'interpreting' || adjustState.status === 'adjusting' ? messages.skills.interpreting
        : versionState.status === 'loading' ? messages.skills.saving : null

  return (
    <>
      <PageHeader
        title={messages.routes.skills.label}
        aside={<span className="scopeBadge skillScopeBadge">
          {projectId ? messages.skills.scopeBadgeWithProject : messages.skills.scopeBadgeNoProject}
        </span>}
      />
      {/* 非活性側も mount を保ち、頁签切替で入力草稿や進行中の要求を破棄しない。 */}
      <div className="tabBar" role="tablist" aria-label={messages.skills.pageTabsAria}>
        <SkillTabButton current={pageTab} tab="library" onSelect={setPageTab}>
          {messages.skills.libraryTitle}
          {libraryState.status === 'ready' && <span className="eventCount">{libraryState.versions.length}</span>}
        </SkillTabButton>
        <SkillTabButton current={pageTab} tab="workbench" onSelect={setPageTab}>
          {messages.skills.tabWorkbench}
        </SkillTabButton>
      </div>
      {pageTab === 'library' && <>
        {importPending && <p role="status">{importPending}</p>}
        {importFailure?.status === 'error' && <p className="error" role="alert">{importFailure.message}</p>}
      </>}
      <div className="tabPanel" role="tabpanel" hidden={pageTab !== 'workbench'}>
      <section className="skillWorkspace" aria-label={messages.skills.workspaceAria}>
        <form className="panel skillForm" onSubmit={(event) => void handleParse(event)}>
          <div className="panelHeader"><h2>{messages.skills.sourceTitle}</h2></div>
          <div className="skillUpload">
            <span>{messages.skills.orUploadDir}</span>
            <label className="primaryButton fileUploadButton">
              {messages.skills.chooseSkillDir}
              <input
                type="file"
                multiple
                aria-label={messages.skills.uploadDirAria}
                ref={(element) => { element?.setAttribute('webkitdirectory', '') }}
                onChange={(event) => {
                  const selected = event.currentTarget.files ? Array.from(event.currentTarget.files) : []
                  event.currentTarget.value = ''
                  void handleUpload(selected)
                }}
              />
            </label>
          </div>
          <p className="hint">{messages.skills.uploadHint}</p>
          {uploadedSource === null ? (
            <details className="detailDisclosure skillTextSource">
              <summary>{messages.skills.manualSource}</summary>
              <label>SKILL.md<textarea placeholder={messages.skills.skillMdPlaceholder} value={skillMarkdown} onChange={(event) => setSkillMarkdown(event.target.value)} spellCheck={false} /></label>
              <details className="detailDisclosure">
                <summary>{messages.skills.referencesLabel}</summary>
                <label>{messages.skills.referencesLabel}<textarea placeholder={messages.skills.referencesPlaceholder} value={referenceMarkdown} onChange={(event) => setReferenceMarkdown(event.target.value)} spellCheck={false} /></label>
              </details>
              <button className="primaryButton" disabled={parseState.status === 'parsing' || !skillMarkdown.trim()} type="submit">{parseState.status === 'parsing' ? messages.skills.parsing : messages.skills.parseSkill}</button>
            </details>
          ) : (
            <UploadedSourceFiles files={uploadedSource} onClear={() => setUploadedSource(null)} />
          )}
        </form>

        <section className="panel skillResult" aria-live="polite">
          <div className="panelHeader">
            <h2>{messages.skills.parseResult}</h2>
            {parseState.status === 'ready' && <span className="scopeBadge">{parseState.result.runtime_manifest_draft.compatibility.level}</span>}
          </div>
          {parseState.status === 'idle' && saveState.status === 'idle' && interpretState.status === 'idle' && <EmptyState text={messages.skills.parseEmptyIdle} />}
          {parseState.status === 'parsing' && <EmptyState text={messages.skills.parseRunning} />}
          {parseState.status === 'error' && <p className="error" role="alert">{parseState.message}</p>}
          {parseState.status === 'ready' && (
            <>
              <SkillParseSummary result={parseState.result} />
              <button className="secondaryButton saveSkillButton" disabled={saveState.status === 'saving'} type="button" onClick={() => void handleSave()}>{saveState.status === 'saving' ? messages.skills.saving : messages.skills.saveResult}</button>
            </>
          )}
          {/* 目录 upload は parseState を経由しないため、保存結果は parse 分岐の外で常に描画する(upload 成功が無反応に見える不具合の修正)。 */}
          {saveState.status === 'saving' && parseState.status !== 'ready' && <EmptyState text={messages.skills.uploadingParsing} />}
          {saveState.status === 'error' && <p className="error" role="alert">{saveState.message}</p>}
          {saveState.status === 'ready' && (
            <>
              <SavedSkillIdentity stored={saveState.stored} />
              <div className="skillActions">
                <button className="secondaryButton" disabled={pendingRequestId !== null || interpretState.status === 'unknown' || interpretState.status === 'interpreting'} type="button" onClick={() => void handleInterpret(saveState.stored.skill_source_id, interpretState.status === 'error')}>{interpretState.status === 'interpreting' ? messages.skills.interpreting : interpretState.status === 'error' ? messages.skills.forceRegenerate : messages.skills.interpretAction}</button>
                <button className="secondaryButton" disabled={versionState.status === 'loading'} type="button" onClick={() => void handleCreateDraft(saveState.stored.interpretation_id)}>{messages.skills.createDraftFromAssisted}</button>
              </div>
            </>
          )}
          {interpretState.status === 'unknown' && (
            <div role="status" className="notice">
              <p>{interpretState.message}</p>
              <div className="skillActions">
                <button className="secondaryButton" type="button" disabled={pendingRequestId === null} onClick={() => void confirmPendingInterpretation()}>{messages.skills.confirmInterpretation}</button>
                <button className="secondaryButton" type="button" onClick={dismissInterpretation}>{messages.skills.dismissInterpretation}</button>
              </div>
            </div>
          )}
          {interpretState.status === 'interpreting' && (
            <InterpretStreamView prompt={interpretState.prompt} output={interpretState.output} attempt={interpretState.attempt} />
          )}
          {interpretState.status === 'error' && <p className="error" role="alert">{interpretState.message}</p>}
          {interpretState.status === 'ready' && (
            <InterpretationExecutionView
              execution={interpretState.execution}
              instruction={instruction}
              onInstructionChange={setInstruction}
              onAdjust={() => void handleAdjust(interpretState.execution.interpretation_id)}
              onRegenerate={() => void handleInterpret(interpretState.execution.skill_source_id, true)}
              onCreateDraft={() => void handleCreateDraft(interpretState.execution.interpretation_id)}
              adjustState={adjustState}
              versionBusy={versionState.status === 'loading'}
            />
          )}
          {versionState.status === 'error' && <p className="error" role="alert">{versionState.message}</p>}
          {versionState.status === 'ready' && <SkillVersionDetail version={versionState.version} onPublish={() => void handlePublish(versionState.version)} />}
        </section>
      </section>
      </div>
      <div className="tabPanel" role="tabpanel" hidden={pageTab !== 'library'}>
        <SkillLibraryPanel
          libraryState={libraryState}
          actionError={libraryActionError}
          onRefresh={() => { setLibraryActionError(null); void refreshLibrary() }}
          enablementState={enablementState}
          projectId={projectId}
          busyVersionId={libraryBusyVersionId}
          onPublish={(version) => void handlePublish(version, true)}
          onDeprecate={(version) => void handleDeprecate(version)}
          onDelete={(version) => void handleDelete(version)}
          onEnable={(version) => void handleEnable(version)}
          onDisable={(version) => void handleDisable(version)}
        />
      </div>
      {confirmDialog}
    </>
  )
}
