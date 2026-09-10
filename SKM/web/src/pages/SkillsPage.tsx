import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'

import {
  ApiProblemError,
  adjustInterpretation,
  createSkillVersionDraft,
  deleteSkillVersion,
  deprecateSkillVersion,
  disableProjectSkillVersion,
  enableProjectSkillVersion,
  interpretSkillSource,
  listProjectSkillVersions,
  listSkillVersions,
  loadInterpretationExecution,
  parseSkillSource,
  publishSkillVersion,
  saveSkillImport,
  subscribeInterpretEvents,
  uploadSkillFiles,
  type InterpretationExecutionRecord,
  type InterpretationLaunchRecord,
  type BlueprintNote,
  type CapabilityBlueprintView,
  type InterpretEventRecord,
  type InterpretEventSubscription,
  type ProjectSkillVersionRecord,
  type SkillDiagnostic,
  type SkillParseResult,
  type SkillSourceFile,
  type SkillVersionRecord,
  type SourceTrace,
  type StoredSkillPreviewRecord,
} from '../api'
import { DetailDrawer, EmptyState, LoadingSkeleton, PageHeader, useConfirmDialog } from '../components/PageElements'
import { useMessages } from '../i18n'
import type { UiMessages } from '../lib/i18n/messages'
import { formatByteSize } from '../lib/presentation'
import { isNearBottom } from '../lib/scroll'
import { normalizedSkillUploadPaths } from '../lib/skillUpload'

/** 上传目录の読取専用 preview entry。text は内容を持ち、binary/過大は種別だけ示す。 */
export interface UploadedSourceFile {
  path: string
  size: number
  kind: 'text' | 'binary' | 'oversized'
  content?: string
}

/** 拡張子で text 判定する許可リスト。mime が text/* の file はこの表になくても text 扱いにする。 */
const TEXT_PREVIEW_EXTENSIONS = new Set([
  'md', 'markdown', 'txt', 'json', 'yaml', 'yml', 'toml', 'csv', 'xml',
  'html', 'htm', 'css', 'js', 'jsx', 'ts', 'tsx', 'py', 'sh',
])

/** 1 file あたりの text preview 上限。超過は内容を読まず oversized として扱う。 */
const TEXT_PREVIEW_MAX_BYTES = 262_144

/** 上传 file 群を preview entry へ変換する。SKILL.md を先頭に固定し、残りは path 昇順。 */
export async function readUploadedSourcePreview(
  files: readonly File[],
): Promise<UploadedSourceFile[]> {
  const paths = normalizedSkillUploadPaths(files)
  const entries = await Promise.all(files.map(async (file, index): Promise<UploadedSourceFile> => {
    const path = paths[index] ?? file.name
    const extension = path.slice(path.lastIndexOf('.') + 1).toLowerCase()
    const isText = file.type.startsWith('text/') || TEXT_PREVIEW_EXTENSIONS.has(extension)
    if (!isText) return { path, size: file.size, kind: 'binary' }
    if (file.size > TEXT_PREVIEW_MAX_BYTES) return { path, size: file.size, kind: 'oversized' }
    return { path, size: file.size, kind: 'text', content: await file.text() }
  }))
  return entries.sort((left, right) => {
    const leftRank = left.path.endsWith('SKILL.md') ? 0 : 1
    const rightRank = right.path.endsWith('SKILL.md') ? 0 : 1
    return leftRank !== rightRank ? leftRank - rightRank : left.path.localeCompare(right.path)
  })
}

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

/** Model interpret/reinterpret の非同期状態。 */
type InterpretState =
  | { status: 'idle' }
  | { status: 'interpreting'; prompt: string; output: string; attempt: number }
  | { status: 'ready'; execution: InterpretationExecutionRecord }
  | { status: 'error'; message: string }

/** 追加調整（reinterpretation）の非同期状態。 */
type AdjustState = { status: 'idle' } | { status: 'adjusting' } | { status: 'error'; message: string }

/** SkillVersion DRAFT 作成・publish の非同期状態。 */
type SkillVersionState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; version: SkillVersionRecord }
  | { status: 'error'; message: string }

/** Organization Skill library 一覧の非同期状態。 */
type SkillLibraryState =
  | { status: 'loading' }
  | { status: 'ready'; versions: SkillVersionRecord[] }
  | { status: 'error'; message: string }

/** 画面の 2 大区分。取込〜発行の作業台と、組織 library の管理を同時に一つだけ見せる。 */
type SkillsPageTab = 'workbench' | 'library'

/** 選択 Project の精確版有効化一覧の非同期状態。 */
type ProjectEnablementState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; enablements: ProjectSkillVersionRecord[] }
  | { status: 'error'; message: string }

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
  const [interpretState, setInterpretState] = useState<InterpretState>({ status: 'idle' })
  const [adjustState, setAdjustState] = useState<AdjustState>({ status: 'idle' })
  const [instruction, setInstruction] = useState('')
  const [versionState, setVersionState] = useState<SkillVersionState>({ status: 'idle' })
  const [libraryState, setLibraryState] = useState<SkillLibraryState>({ status: 'loading' })
  const [enablementState, setEnablementState] = useState<ProjectEnablementState>({ status: 'idle' })
  const [libraryBusyVersionId, setLibraryBusyVersionId] = useState<string | null>(null)
  const [pageTab, setPageTab] = useState<SkillsPageTab>('workbench')
  const { confirm, confirmDialog } = useConfirmDialog()
  const parseController = useRef<AbortController | null>(null)
  const saveController = useRef<AbortController | null>(null)
  const uploadController = useRef<AbortController | null>(null)
  const interpretController = useRef<AbortController | null>(null)
  const versionController = useRef<AbortController | null>(null)
  const libraryController = useRef<AbortController | null>(null)
  const enablementController = useRef<AbortController | null>(null)
  const libraryMutationController = useRef<AbortController | null>(null)
  const interpretStream = useRef<InterpretEventSubscription | null>(null)

  useEffect(() => () => {
    parseController.current?.abort()
    saveController.current?.abort()
    uploadController.current?.abort()
    interpretController.current?.abort()
    versionController.current?.abort()
    libraryController.current?.abort()
    enablementController.current?.abort()
    libraryMutationController.current?.abort()
    interpretStream.current?.close()
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
    setLibraryState({ status: 'loading' })
    try {
      const versions = await listSkillVersions(controller.signal)
      if (!controller.signal.aborted) setLibraryState({ status: 'ready', versions })
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setLibraryState({
          status: 'error',
          message: error instanceof Error ? error.message : messages.skills.loadLibraryFailed,
        })
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
          message: error instanceof Error ? error.message : messages.skills.loadEnablementsFailed,
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

  /** 後続 state を idle へ戻し、古い解釈/版本を残さないようにする。 */
  function resetDownstream(): void {
    setInterpretState({ status: 'idle' })
    setAdjustState({ status: 'idle' })
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
      setParseState({
        status: 'ready',
        result: await parseSkillSource(currentFiles(), csrfToken, controller.signal),
      })
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setParseState({ status: 'error', message: error instanceof Error ? error.message : 'Unknown parser error' })
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
      setSaveState({
        status: 'ready',
        stored: await saveSkillImport(currentFiles(), csrfToken, controller.signal),
      })
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setSaveState({ status: 'error', message: error instanceof Error ? error.message : 'Unknown persistence error' })
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
        setSaveState({ status: 'error', message: error instanceof Error ? error.message : 'Unknown upload error' })
      }
    }
  }

  /** launch 受理後、queued なら SSE 進行を購読し、終端で最終 execution を取得する。 */
  function driveLaunch(launch: InterpretationLaunchRecord): void {
    interpretStream.current?.close()
    if (launch.status === 'stored' && launch.execution !== null) {
      setInterpretState({ status: 'ready', execution: launch.execution })
      return
    }
    // Worker 実行待ち。prompt と出力 delta を SSE で累積表示する。
    setInterpretState({ status: 'interpreting', prompt: '', output: '', attempt: 0 })
    const subscription = subscribeInterpretEvents(
      launch.execution_key,
      (event) => { void handleInterpretEvent(event) },
      () => {
        // 接続 error は表示品質の劣化に留め、終端は最終 execution 取得側で確定させる。
      },
    )
    interpretStream.current = subscription
  }

  /** SSE 進行 event を状態へ反映し、終端 event で最終結果を取得する。 */
  async function handleInterpretEvent(event: InterpretEventRecord): Promise<void> {
    if (event.event === 'interpret.prompt') {
      const system = typeof event.data.system_prompt === 'string' ? event.data.system_prompt : ''
      const user = typeof event.data.user_message === 'string' ? event.data.user_message : ''
      // 各 attempt の冒頭で prompt を差し替え、前 attempt の出力を消し、試行回数を進める(retry を可視化)。
      setInterpretState((current) => current.status === 'interpreting'
        ? { ...current, prompt: `${system}\n\n---\n\n${user}`, output: '', attempt: current.attempt + 1 }
        : current)
      return
    }
    if (event.event === 'interpret.delta') {
      const text = typeof event.data.text === 'string' ? event.data.text : ''
      setInterpretState((current) => current.status === 'interpreting'
        ? { ...current, output: current.output + text }
        : current)
      return
    }
    if (event.event === 'interpret.completed' || event.event === 'interpret.failed') {
      interpretStream.current?.close()
      interpretStream.current = null
      const interpretationId = event.data.interpretation_id
      if (event.event === 'interpret.failed' || typeof interpretationId !== 'string') {
        const code = typeof event.data.error_code === 'string' ? event.data.error_code : 'unknown'
        setInterpretState({ status: 'error', message: messages.skills.interpretFailedCode(code) })
        return
      }
      try {
        const execution = await loadInterpretationExecution(interpretationId)
        setInterpretState({ status: 'ready', execution })
      } catch (error: unknown) {
        setInterpretState({ status: 'error', message: error instanceof Error ? error.message : 'Unknown interpret error' })
      }
    }
  }

  /** 保存済み source の model 解釈を受理し、進行を SSE で観測する。 */
  async function handleInterpret(
    skillSourceId: string,
    forceRegenerate = false,
  ): Promise<void> {
    interpretController.current?.abort()
    const controller = new AbortController()
    interpretController.current = controller
    setInterpretState({ status: 'interpreting', prompt: '', output: '', attempt: 0 })
    setAdjustState({ status: 'idle' })
    setVersionState({ status: 'idle' })
    try {
      const launch = await interpretSkillSource(
        skillSourceId,
        csrfToken,
        controller.signal,
        forceRegenerate,
      )
      if (!controller.signal.aborted) driveLaunch(launch)
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setInterpretState({ status: 'error', message: error instanceof Error ? error.message : 'Unknown interpret error' })
      }
    }
  }

  /** 親解釈へ追加調整を適用し、進行を SSE で観測する。 */
  async function handleAdjust(interpretationId: string): Promise<void> {
    const text = instruction.trim()
    if (!text) return
    interpretController.current?.abort()
    const controller = new AbortController()
    interpretController.current = controller
    setAdjustState({ status: 'adjusting' })
    setVersionState({ status: 'idle' })
    try {
      const launch = await adjustInterpretation(
        interpretationId,
        text,
        csrfToken,
        controller.signal,
      )
      if (controller.signal.aborted) return
      setInstruction('')
      setAdjustState({ status: 'idle' })
      driveLaunch(launch)
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setAdjustState({ status: 'error', message: error instanceof Error ? error.message : 'Unknown adjust error' })
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
      setVersionState({ status: 'ready', version })
      await refreshLibrary()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setVersionState({ status: 'error', message: error instanceof Error ? error.message : 'Unknown draft API error' })
      }
    }
  }

  /** Hard gate 通過済み DRAFT だけを publish API へ送る。 */
  async function handlePublish(version: SkillVersionRecord): Promise<void> {
    if (!version.gate_passed) return
    versionController.current?.abort()
    const controller = new AbortController()
    versionController.current = controller
    setVersionState({ status: 'loading' })
    try {
      const warnings = version.gate_findings
        .filter((finding) => finding.severity === 'warning')
        .map((finding) => finding.code)
      if (warnings.length > 0 && !await confirm({
        title: messages.skills.publishVersion,
        message: messages.skills.publishWarningsConfirm(warnings),
        confirmLabel: messages.skills.publishVersion,
      })) {
        setVersionState({ status: 'ready', version })
        return
      }
      setVersionState({
        status: 'ready',
        version: await publishSkillVersion(
          version.skill_version_id,
          warnings,
          csrfToken,
          controller.signal,
        ),
      })
      await refreshLibrary()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setVersionState({ status: 'error', message: error instanceof Error ? error.message : 'Unknown publish API error' })
      }
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
    setLibraryBusyVersionId(version.skill_version_id)
    try {
      await deprecateSkillVersion(version.skill_version_id, csrfToken, controller.signal)
      await refreshLibrary()
      await refreshEnablements()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setLibraryState({
          status: 'error',
          message: error instanceof Error ? error.message : messages.skills.deprecateFailed,
        })
      }
    } finally {
      if (!controller.signal.aborted) setLibraryBusyVersionId(null)
    }
  }

  /** 監査参照のない DEPRECATED 版を organization library から取り除く。
   *
   * Run snapshot や composition から参照されている版は backend が 409 で拒否する。
   * その場合は「なぜ消せないか」を一覧の error 欄へ出し、利用者が諦め方を判断できるようにする。
   */
  async function handleDelete(version: SkillVersionRecord): Promise<void> {
    if (version.status !== 'DEPRECATED') return
    if (!await confirm({
      title: messages.skills.deleteVersion,
      message: messages.skills.deleteConfirm(version.name, version.version),
      confirmLabel: messages.skills.deleteVersion,
      destructive: true,
    })) return
    libraryMutationController.current?.abort()
    const controller = new AbortController()
    libraryMutationController.current = controller
    setLibraryBusyVersionId(version.skill_version_id)
    try {
      await deleteSkillVersion(version.skill_version_id, csrfToken, controller.signal)
      await refreshLibrary()
      await refreshEnablements()
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setLibraryState({
          status: 'error',
          message: error instanceof ApiProblemError && error.code === 'skill_version_delete_blocked'
            ? messages.skills.deleteBlocked
            : error instanceof Error ? error.message : messages.skills.deleteFailed,
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
          message: error instanceof Error ? error.message : messages.skills.enableFailed,
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
          message: error instanceof Error ? error.message : messages.skills.disableFailed,
        })
      }
    } finally {
      if (!controller.signal.aborted) setLibraryBusyVersionId(null)
    }
  }

  return (
    <>
      <PageHeader
        title={messages.routes.skills.label}
        description={messages.skills.description}
        aside={<span className="scopeBadge">
          {projectId ? messages.skills.scopeBadgeWithProject : messages.skills.scopeBadgeNoProject}
        </span>}
      />
      {/* 取込〜発行の作業台と組織 library を tab で分け、1 画面の縦積みを解消する。
          非活性側も hidden で DOM に残す(頁面測試の toContain と状態保持のため)。 */}
      <div className="tabBar" role="tablist" aria-label={messages.skills.pageTabsAria}>
        <SkillsPageTabButton current={pageTab} tab="workbench" onSelect={setPageTab}>
          {messages.skills.tabWorkbench}
        </SkillsPageTabButton>
        <SkillsPageTabButton current={pageTab} tab="library" onSelect={setPageTab}>
          {messages.skills.libraryTitle}
          {libraryState.status === 'ready' && <span className="eventCount">{libraryState.versions.length}</span>}
        </SkillsPageTabButton>
      </div>
      <div className="tabPanel" role="tabpanel" hidden={pageTab !== 'workbench'}>
      <section className="skillWorkspace" aria-label={messages.skills.workspaceAria}>
        <form className="panel skillForm" onSubmit={(event) => void handleParse(event)}>
          <div className="panelHeader"><h2>{messages.skills.sourceTitle}</h2></div>
          {uploadedSource === null ? (
            <>
              <label>SKILL.md<textarea placeholder={messages.skills.skillMdPlaceholder} value={skillMarkdown} onChange={(event) => setSkillMarkdown(event.target.value)} spellCheck={false} /></label>
              <details className="detailDisclosure">
                <summary>{messages.skills.referencesLabel}</summary>
                <label>{messages.skills.referencesLabel}<textarea placeholder={messages.skills.referencesPlaceholder} value={referenceMarkdown} onChange={(event) => setReferenceMarkdown(event.target.value)} spellCheck={false} /></label>
              </details>
              <p className="hint">{messages.skills.parserHint}</p>
              <button className="primaryButton" disabled={parseState.status === 'parsing' || !skillMarkdown.trim()} type="submit">{parseState.status === 'parsing' ? messages.skills.parsing : messages.skills.parseSkill}</button>
            </>
          ) : (
            <UploadedSourceFiles files={uploadedSource} onClear={() => setUploadedSource(null)} />
          )}
          <div className="skillUpload">
            <span>{messages.skills.orUploadDir}</span>
            <label className="secondaryButton fileUploadButton">
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
        </form>

        <section className="panel skillResult" aria-live="polite">
          <div className="panelHeader">
            <h2>{messages.skills.parseResult}</h2>
            {parseState.status === 'ready' && <span className="scopeBadge">{parseState.result.runtime_manifest_draft.compatibility.level}</span>}
          </div>
          {parseState.status === 'idle' && saveState.status === 'idle' && <EmptyState text={messages.skills.parseEmptyIdle} />}
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
                <button className="secondaryButton" disabled={interpretState.status === 'interpreting'} type="button" onClick={() => void handleInterpret(saveState.stored.skill_source_id, interpretState.status === 'error')}>{interpretState.status === 'interpreting' ? messages.skills.interpreting : interpretState.status === 'error' ? messages.skills.forceRegenerate : messages.skills.interpretAction}</button>
                <button className="secondaryButton" disabled={versionState.status === 'loading'} type="button" onClick={() => void handleCreateDraft(saveState.stored.interpretation_id)}>{messages.skills.createDraftFromAssisted}</button>
              </div>
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
                  onRegenerate={() => void handleInterpret(saveState.stored.skill_source_id, true)}
                  onCreateDraft={() => void handleCreateDraft(interpretState.execution.interpretation_id)}
                  adjustState={adjustState}
                  versionBusy={versionState.status === 'loading'}
                />
              )}
              {versionState.status === 'error' && <p className="error" role="alert">{versionState.message}</p>}
              {versionState.status === 'ready' && <SkillVersionDetail version={versionState.version} onPublish={() => void handlePublish(versionState.version)} />}
            </>
          )}
        </section>
      </section>
      </div>
      <div className="tabPanel" role="tabpanel" hidden={pageTab !== 'library'}>
        <SkillLibraryPanel
          libraryState={libraryState}
          enablementState={enablementState}
          projectId={projectId}
          busyVersionId={libraryBusyVersionId}
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

/** 画面 2 大区分の tab button。選択状態を aria-selected で表す。 */
function SkillsPageTabButton({ current, tab, onSelect, children }: {
  current: SkillsPageTab
  tab: SkillsPageTab
  onSelect: (tab: SkillsPageTab) => void
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

/** Organization version 一覧と選択 Project の明示有効化関係を同じ精確版単位で表示する。 */
export function SkillLibraryPanel({
  libraryState,
  enablementState,
  projectId,
  busyVersionId,
  onDeprecate,
  onDelete,
  onEnable,
  onDisable,
}: {
  libraryState: SkillLibraryState
  enablementState: ProjectEnablementState
  projectId: string
  busyVersionId: string | null
  onDeprecate: (version: SkillVersionRecord) => void
  onDelete: (version: SkillVersionRecord) => void
  onEnable: (version: SkillVersionRecord) => void
  onDisable: (version: SkillVersionRecord) => void
}) {
  const messages = useMessages()
  const enablements = enablementState.status === 'ready' ? enablementState.enablements : []
  const activeIds = new Set(enablements
    .filter(({ disabled_at }) => disabled_at === null)
    .map(({ skill_version }) => skill_version.skill_version_id))
  const disabledIds = new Set(enablements
    .filter(({ disabled_at }) => disabled_at !== null)
    .map(({ skill_version }) => skill_version.skill_version_id))

  return (
    <section className="panel skillLibrary" aria-label={messages.skills.libraryAria}>
      <div className="panelHeader">
        <div>
          <h2>{messages.skills.libraryTitle}</h2>
          <p className="hint">{messages.skills.libraryHint}</p>
        </div>
        {projectId
          ? <span className="scopeBadge">{messages.skills.enabledCount(activeIds.size)}</span>
          : <span className="scopeBadge">{messages.skills.noProjectBadge}</span>}
      </div>
      {!projectId && <p className="hint">{messages.skills.libraryNoProjectHint}</p>}
      {enablementState.status === 'error' && <p className="error" role="alert">{enablementState.message}</p>}
      {/* 読み込み中は「空(点線枠)」ではなく骨格行を出す。空態と loading の意味を取り違えさせない。 */}
      {(libraryState.status === 'loading' || enablementState.status === 'loading') && (
        <LoadingSkeleton
          label={libraryState.status === 'loading' ? messages.skills.loadingLibrary : messages.skills.loadingEnablements}
          rows={3}
        />
      )}
      {libraryState.status === 'error' && <p className="error" role="alert">{libraryState.message}</p>}
      {libraryState.status === 'ready' && libraryState.versions.length === 0 && (
        <EmptyState text={messages.skills.emptyLibrary} />
      )}
      {libraryState.status === 'ready' && libraryState.versions.length > 0 && (
        <ul className="skillLibraryList">
          {libraryState.versions.map((version) => {
            const active = activeIds.has(version.skill_version_id)
            const disabled = disabledIds.has(version.skill_version_id)
            const busy = busyVersionId === version.skill_version_id
            return (
              <li key={version.skill_version_id}>
                {/* 内部 UUID は利用者の判断材料にならないため出さない。読める識別は
                    「名称 + 版 + SKILL.md 原文の説明」で足り、skill_key は追跡用に残す。 */}
                <div className="skillLibraryIdentity">
                  <strong>{version.name} <span className="mono">v{version.version}</span></strong>
                  {version.description && <p className="skillLibraryDescription">{version.description}</p>}
                  <span className="mono">{version.skill_key}</span>
                </div>
                <div className="skillLibraryStatus">
                  <span className="statusBadge">{messages.enums.skillVersionStatus[version.status] ?? version.status}</span>
                  {projectId && active && <span className="scopeBadge">{messages.skills.enabledBadge}</span>}
                  {projectId && disabled && <span className="scopeBadge">{messages.skills.disabledBadge}</span>}
                </div>
                <div className="skillActions">
                  {projectId && active && (
                    <button className="secondaryButton" type="button" disabled={busy} onClick={() => onDisable(version)}>
                      {busy ? messages.elements.processing : messages.skills.disableFromProject}
                    </button>
                  )}
                  {projectId && !active && !disabled && version.status === 'PUBLISHED' && (
                    <button className="primaryButton" type="button" disabled={busy} onClick={() => onEnable(version)}>
                      {busy ? messages.elements.processing : messages.skills.enableForProject}
                    </button>
                  )}
                  {projectId && disabled && (
                    <span className="hint">{messages.skills.disabledAuditHint}</span>
                  )}
                  {version.status === 'PUBLISHED' && (
                    <button className="secondaryButton" type="button" disabled={busy} onClick={() => onDeprecate(version)}>
                      {busy ? messages.elements.processing : messages.skills.deprecateVersion}
                    </button>
                  )}
                  {/* 廃止しただけでは行が残り続けるため、監査参照のない版に限り片付け経路を出す。
                      参照が残る版は backend が 409 で拒否し、その理由を一覧の error 欄へ出す。 */}
                  {version.status === 'DEPRECATED' && (
                    <button className="dangerButton" type="button" disabled={busy} onClick={() => onDelete(version)}>
                      {busy ? messages.elements.processing : messages.skills.deleteVersion}
                    </button>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

/** 上传済み目录の内容を「Skill 源文件」module 内で確認する読取専用 preview。 */
export function UploadedSourceFiles({ files, onClear }: {
  files: UploadedSourceFile[]
  onClear: () => void
}) {
  const messages = useMessages()
  return (
    <div className="sourcePreview">
      <div className="sourcePreviewHeader">
        <span>{messages.skills.uploadedCount(files.length)}</span>
        <button className="secondaryButton compactButton" type="button" onClick={onClear}>{messages.skills.clearUseManual}</button>
      </div>
      <ul className="sourcePreviewList">
        {files.map((file) => (
          <li key={file.path}>
            {file.kind === 'text' ? (
              <details className="sourcePreviewFile" open={file.path.endsWith('SKILL.md')}>
                <summary><code>{file.path}</code><span>{formatByteSize(file.size)}</span></summary>
                <pre>{file.content}</pre>
              </details>
            ) : (
              <div className="sourcePreviewOpaque">
                <code>{file.path}</code>
                <span>
                  {file.kind === 'binary' ? messages.skills.binaryStored : messages.skills.textTooLarge}
                  {' · '}{formatByteSize(file.size)}
                </span>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Worker 実行中の model 解釈を、実際の prompt と流式出力として表示する。 */
export function InterpretStreamView({ prompt, output, attempt = 1 }: {
  prompt: string
  output: string
  attempt?: number
}) {
  const messages = useMessages()
  const outputRef = useRef<HTMLPreElement>(null)
  const pinnedToBottom = useRef(true)
  // 出力の伸長に追随して末尾へスクロールする。ただしユーザーが上へ離れている間は追随しない。
  useEffect(() => {
    const element = outputRef.current
    if (element && pinnedToBottom.current) element.scrollTop = element.scrollHeight
  }, [output])
  return (
    <div className="interpretStream" aria-live="polite">
      <div className="interpretStreamHead">
        <span className="streamDot" aria-hidden="true" />
        <strong>{messages.skills.interpretRunning}</strong>
        <span className="interpretStreamHint">{messages.skills.interpretRunningHint}</span>
      </div>
      {attempt > 1 && (
        <p className="hint" role="status">
          {messages.skills.attemptLine(attempt)}
        </p>
      )}
      {prompt !== '' && (
        <details className="interpretPrompt">
          <summary>{messages.skills.promptSent}</summary>
          <pre>{prompt}</pre>
        </details>
      )}
      <div className="interpretOutput">
        <span>{messages.skills.modelOutput}</span>
        {output === ''
          ? <p className="interpretOutputWaiting">{messages.skills.waitingModelOutput}</p>
          : (
            <pre ref={outputRef} onScroll={(event) => { pinnedToBottom.current = isNearBottom(event.currentTarget) }}>
              {output}<span className="streamCursor" />
            </pre>
          )}
      </div>
    </div>
  )
}

/** 解釈詳細内の観測区分。要約と操作は tab の外に常置し、詳細だけを切り替える。 */
type InterpretationDetailTab = 'report' | 'blueprint' | 'contracts' | 'diff'

/** Model interpretation の report、source trace、confidence、diff、追加調整を表示する。
 *
 *  詳細(報告・蓝图・契約・差分)は縦へ全部積むと発行判断に要る要約と操作が埋もれるため、
 *  tab で同時に一つだけ見せる。非活性 tab も hidden で mount したままにする(測試断言と状態保持)。 */
export function InterpretationExecutionView({ execution, instruction, onInstructionChange, onAdjust, onRegenerate, onCreateDraft, adjustState, versionBusy }: {
  execution: InterpretationExecutionRecord
  instruction: string
  onInstructionChange: (value: string) => void
  onAdjust: () => void
  onRegenerate: () => void
  onCreateDraft: () => void
  adjustState: AdjustState
  versionBusy: boolean
}) {
  const messages = useMessages()
  const [detailTab, setDetailTab] = useState<InterpretationDetailTab>('report')
  const report = execution.report
  const failed = execution.status !== 'PREVIEW_READY'
  const parentInstruction = readAdjustmentInstruction(execution.adjustment)
  const blueprint = execution.preview.capability_blueprint
  const hasBlueprint = blueprint !== null && (blueprint.capabilities.length > 0 || blueprint.tasks.length > 0)
  const manifest = execution.preview.runtime_manifest_draft
  const hasContracts = Array.isArray(manifest.tasks) && manifest.tasks.some(isPlainRecord)
  return (
    <section className="interpretationPanel">
      <div className="subsectionHeader">
        <h3>{messages.skills.interpretationTitle}</h3>
        <span className="scopeBadge">{execution.compatibility_level}</span>
      </div>
      <dl className="runFacts">
        <div><dt>{messages.skills.interpretationIdLabel}</dt><dd className="mono">{execution.interpretation_id}</dd></div>
        <div><dt>{messages.skills.interpretationStatusLabel}</dt><dd>{execution.status}{execution.reused ? messages.skills.reusedSuffix : ''}</dd></div>
        <div><dt>{messages.skills.interpretationModelLabel}</dt><dd className="mono">{execution.model ?? '—'}</dd></div>
        <div><dt>{messages.skills.interpretationConfidenceLabel}</dt><dd>{execution.confidence.toFixed(2)}</dd></div>
      </dl>
      {failed && <p className="error" role="alert">{messages.skills.interpretationFailedLine(execution.error_code ?? 'unknown')}</p>}
      {execution.parent_interpretation_id && (
        <p className="hint">{messages.skills.parentPrefix}<code className="mono">{execution.parent_interpretation_id}</code>{parentInstruction ? messages.skills.adjustQuote(parentInstruction) : ''}</p>
      )}
      {report && <p className="interpretationSummary">{report.summary}</p>}
      <div className="tabBar" role="tablist" aria-label={messages.skills.detailTabsAria}>
        <InterpretationTabButton current={detailTab} tab="report" onSelect={setDetailTab}>{messages.skills.tabReport}</InterpretationTabButton>
        <InterpretationTabButton current={detailTab} tab="blueprint" onSelect={setDetailTab}>{messages.skills.blueprintTitle}</InterpretationTabButton>
        <InterpretationTabButton current={detailTab} tab="contracts" onSelect={setDetailTab}>{messages.skills.generatedContractsTitle}</InterpretationTabButton>
        <InterpretationTabButton current={detailTab} tab="diff" onSelect={setDetailTab}>
          {messages.skills.revisionDiffTitle}
          {/* 構造差分がある時だけ点を出し、他 tab からも「見るべき差分がある」ことを示す。 */}
          {execution.diff.has_changes === true && <i className="tabAlert" aria-hidden="true" />}
        </InterpretationTabButton>
      </div>
      <div className="tabPanel" role="tabpanel" hidden={detailTab !== 'report'}>
        {report ? (
          <div className="interpretationDetailStack">
            <ConfidenceGrid confidence={report.confidence} />
            <NoteBlock title={messages.skills.assumptionsTitle} items={report.assumptions} />
            <NoteBlock title={messages.skills.questionsTitle} items={report.questions.map((q) => ({ key: q.key, text: q.required ? `${q.text}${messages.skills.requiredAnswerSuffix}` : q.text }))} />
            {report.source_traces.length > 0 && (
              <div className="noteBlock">
                <h4>{messages.skills.sourceTracesTitle}</h4>
                <ul className="sourceTraceList">
                  {report.source_traces.map((trace, index) => <SourceTraceItem key={`${trace.target}-${index}`} trace={trace} />)}
                </ul>
              </div>
            )}
            {report.diagnostics.length > 0 && (
              <ul className="diagnostics">
                {report.diagnostics.map((diagnostic, index) => (
                  <li key={`${diagnostic.code}-${index}`}>
                    <strong>{diagnostic.severity} · {diagnostic.code}</strong>
                    <span>{diagnostic.message}{diagnostic.path ? ` (${diagnostic.path}${diagnostic.line ? `:${diagnostic.line}` : ''})` : ''}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : <p className="hint">{messages.skills.reportEmpty}</p>}
      </div>
      <div className="tabPanel" role="tabpanel" hidden={detailTab !== 'blueprint'}>
        {hasBlueprint
          ? <CapabilityBlueprintPreview blueprint={blueprint} />
          : <p className="hint">{messages.skills.blueprintEmpty}</p>}
      </div>
      <div className="tabPanel" role="tabpanel" hidden={detailTab !== 'contracts'}>
        {hasContracts
          ? <GeneratedContractPreview manifest={manifest} />
          : <p className="hint">{messages.skills.contractsEmpty}</p>}
      </div>
      <div className="tabPanel" role="tabpanel" hidden={detailTab !== 'diff'}>
        <RevisionDiffView diff={execution.diff} hasParent={execution.parent_interpretation_id !== null} />
      </div>
      <form className="adjustForm" onSubmit={(event) => { event.preventDefault(); onAdjust() }}>
        <label>{messages.skills.adjustLabel}<textarea className="compactTextarea" value={instruction} onChange={(event) => onInstructionChange(event.target.value)} placeholder={messages.skills.adjustPlaceholder} spellCheck={false} /></label>
        {adjustState.status === 'error' && <p className="error" role="alert">{adjustState.message}</p>}
        <div className="skillActions">
          <button className="secondaryButton" type="submit" disabled={adjustState.status === 'adjusting' || !instruction.trim()}>{adjustState.status === 'adjusting' ? messages.skills.adjusting : messages.skills.adjustAndReinterpret}</button>
          <button className="secondaryButton" type="button" disabled={versionBusy} onClick={onRegenerate}>{messages.skills.forceRegenerate}</button>
          <button className="primaryButton" type="button" disabled={failed || versionBusy} onClick={onCreateDraft}>{versionBusy ? messages.skills.processing : messages.skills.createDraftFromThis}</button>
        </div>
      </form>
    </section>
  )
}

/** 解釈詳細 tab の button。選択状態を aria-selected で表す。 */
function InterpretationTabButton({ current, tab, onSelect, children }: {
  current: InterpretationDetailTab
  tab: InterpretationDetailTab
  onSelect: (tab: InterpretationDetailTab) => void
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

/** Skill の能力・目標・資源・規則・交付物・効果を業務固有分岐なしで公開前に示す。
 *
 * 業務 Schema と gate finding だけでは「この Skill が何をできるのか」が読めない。蓝图は
 * 資源前提と効果意図を明示し、効果が意図であって権限ではないことを利用者へ示す。
 */
function CapabilityBlueprintPreview({ blueprint }: { blueprint: CapabilityBlueprintView | null }) {
  // 蓝图は Interpreter の産物であり、解釈前は存在しない。空の枠を出すより、まだ無いことを
  // 示さないほうが「解釈したのに能力を抽出できなかった」との誤読を避けられる。
  const messages = useMessages()
  if (blueprint === null) return null
  const { capabilities, tasks, resource_requirements: resources, guidance } = blueprint
  if (capabilities.length === 0 && tasks.length === 0) return null
  return (
    <div className="noteBlock">
      {/* 見出しは親の詳細 tab(能力蓝图)が担うため、ここでは重複させない。 */}
      {capabilities.map((capability) => (
        <section key={capability.key}>
          <strong>{capability.title}</strong>
          <span className="mono">{capability.key}</span>
          {capability.summary && <p className="hint">{capability.summary}</p>}
        </section>
      ))}
      {tasks.map((task) => (
        <section key={task.key}>
          <strong>{messages.skills.objectivePrefix(task.key)}</strong>
          <p className="hint">{task.objective}</p>
          <BlueprintNoteList title={messages.skills.successCriteria} notes={task.success_criteria ?? []} />
          {(task.deliverables ?? []).length > 0 && (
            <ul className="noteList">
              {(task.deliverables ?? []).map((deliverable) => (
                <li key={deliverable.key}>
                  <strong>{messages.skills.deliverablePrefix(deliverable.kind)}</strong>
                  <span>{deliverable.description}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      ))}
      {resources.length > 0 && (
        <section>
          <strong>{messages.skills.resourcePrereq}</strong>
          <ul className="noteList">
            {resources.map((resource) => (
              <li key={resource.key}>
                <strong>{resource.key} · {resource.kind}</strong>
                <span>
                  {resource.required ? messages.skills.requiredLabel : messages.skills.optionalLabel} · {resource.access}
                  {(resource.capabilities ?? []).length > 0
                    ? ` · ${(resource.capabilities ?? []).join(', ')}`
                    : ''}
                  {resource.selection_guidance ? ` — ${resource.selection_guidance}` : ''}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <BlueprintNoteList title={messages.skills.requiredRules} notes={guidance.required_rules} />
      <BlueprintNoteList title={messages.skills.recommendedSteps} notes={guidance.recommended_steps} />
      <BlueprintNoteList title={messages.skills.qualityCriteria} notes={guidance.quality_criteria} />
      <BlueprintNoteList title={messages.skills.prohibited} notes={guidance.prohibited_actions} />
      {blueprint.interaction_points.length > 0 && (
        <section>
          <strong>{messages.skills.interactionPoints}</strong>
          <ul className="noteList">
            {blueprint.interaction_points.map((point) => (
              <li key={point.key}>
                <strong>{point.type}</strong>
                <span>{point.condition}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {blueprint.effect_intents.length > 0 && (
        <section>
          <strong>{messages.skills.effectIntents}</strong>
          <ul className="noteList">
            {blueprint.effect_intents.map((intent) => (
              <li key={intent.key}>
                <strong>{intent.mode} · risk {intent.risk}</strong>
                <span>
                  {intent.operation}
                  {intent.resource_key ? ` · ${intent.resource_key}` : ''}
                </span>
              </li>
            ))}
          </ul>
          <p className="hint">{messages.skills.effectIntentHint}</p>
        </section>
      )}
    </div>
  )
}

/** 蓝图の note 群を空なら描画せずに一覧化する。 */
function BlueprintNoteList({ title, notes }: { title: string; notes: BlueprintNote[] }) {
  if (notes.length === 0) return null
  return (
    <div>
      <span>{title}</span>
      <ul className="noteList">
        {notes.map((note) => (
          <li key={note.key}>
            <strong>{note.key}</strong>
            <span>{note.text}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Interpreter が生成し platform がコンパイルした task field を公開前に確認可能にする。 */
function GeneratedContractPreview({ manifest }: { manifest: Record<string, unknown> }) {
  const messages = useMessages()
  const tasks = Array.isArray(manifest.tasks)
    ? manifest.tasks.filter(isPlainRecord)
    : []
  if (tasks.length === 0) return null
  return (
    <div className="noteBlock">
      {/* 見出しは親の詳細 tab(生成的任务契约)が担うため、ここでは重複させない。 */}
      {tasks.map((task, index) => (
        <section key={typeof task.key === 'string' ? task.key : index}>
          <strong>{typeof task.key === 'string' ? task.key : `task-${index + 1}`}</strong>
          <ContractFieldList contract={task.input_contract} title={messages.skills.contractInputTitle} />
          <ContractFieldList contract={task.output_contract} title={messages.skills.contractOutputTitle} />
        </section>
      ))}
    </div>
  )
}

/** TaskContractDraft の field/type/required を business 固有分岐なしで一覧化する。 */
function ContractFieldList({ contract, title }: { contract: unknown; title: string }) {
  const messages = useMessages()
  if (!isPlainRecord(contract)) return null
  const fields = Array.isArray(contract.fields) ? contract.fields.filter(isPlainRecord) : []
  return (
    <div>
      <span>{title} · {String(contract.type ?? 'unknown')}</span>
      {fields.length > 0 && (
        <ul className="noteList">
          {fields.map((field, index) => (
            <li key={typeof field.key === 'string' ? field.key : index}>
              <strong>{String(field.key ?? index)}</strong>
              <span>{String(field.type ?? 'unknown')} · {field.required === true ? messages.skills.contractFieldRequired : messages.skills.contractFieldOptional}{typeof field.description === 'string' ? ` — ${field.description}` : ''}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** 分項 confidence を小さな metric grid で表示する。 */
function ConfidenceGrid({ confidence }: { confidence: Record<string, number> }) {
  const entries = Object.entries(confidence)
  if (entries.length === 0) return null
  return (
    <div className="resourceGrid">
      {entries.map(([area, value]) => (
        <div className="metric" key={area}><span>{area}</span><strong>{value.toFixed(2)}</strong></div>
      ))}
    </div>
  )
}

/** key/text の note list（assumptions・questions）を表示する。 */
function NoteBlock({ title, items }: { title: string; items: Array<{ key: string; text: string }> }) {
  if (items.length === 0) return null
  return (
    <div className="noteBlock">
      <h4>{title}</h4>
      <ul className="noteList">
        {items.map((item, index) => (
          <li key={`${item.key}-${index}`}><strong>{item.key}</strong><span>{item.text}</span></li>
        ))}
      </ul>
    </div>
  )
}

/** 一つの source trace を target と位置付きで表示する。 */
function SourceTraceItem({ trace }: { trace: SourceTrace }) {
  return (
    <li>
      <code className="mono">{trace.target}</code>
      <span>{trace.path}{trace.line ? `:${trace.line}` : ''} — {trace.reason}</span>
    </li>
  )
}

/** 親との revision diff を dimension 別に要約し、原始 JSON も併記する。 */
function RevisionDiffView({ diff, hasParent }: { diff: Record<string, unknown>; hasParent: boolean }) {
  const messages = useMessages()
  const lines = summarizeDiff(messages, diff)
  const changed = diff.has_changes === true && lines.length > 0
  return (
    <div className="revisionDiff">
      {/* 見出しは親の詳細 tab(修订差异)が担う。差分有無の badge だけをここに残す。 */}
      {changed && <span className="scopeBadge">{messages.skills.hasDiff}</span>}
      {!hasParent && <p className="hint">{messages.skills.firstInterpretation}</p>}
      {hasParent && !changed && <p className="hint">{messages.skills.noStructuralDiff}</p>}
      {changed && <ul className="diffLines">{lines.map((line, index) => <li key={index}>{line}</li>)}</ul>}
      {hasParent && (
        <details className="rawResult"><summary>{messages.skills.viewRawDiff}</summary><pre>{JSON.stringify(diff, null, 2)}</pre></details>
      )}
    </div>
  )
}

/** Frozen Manifest identity、diff、gate finding と publish control を表示する。 */
export function SkillVersionDetail({ version, onPublish }: {
  version: SkillVersionRecord
  onPublish: () => void
}) {
  const messages = useMessages()
  return (
    <section className="skillVersionDetail">
      <div className="subsectionHeader"><h3>{messages.skills.versionHeading(version.version)}</h3><span>{messages.enums.skillVersionStatus[version.status] ?? version.status}</span></div>
      <dl className="runFacts"><div><dt>{messages.skills.gateLabel}</dt><dd>{version.gate_passed ? messages.skills.gatePassed : messages.skills.gateFailed}</dd></div></dl>
      <DetailDrawer title={messages.elements.technicalDetails}>
        <dl className="runFacts"><div><dt>{messages.skills.versionIdLabel}</dt><dd className="mono">{version.skill_version_id}</dd></div><div><dt>{messages.skills.manifestChecksumLabel}</dt><dd className="mono">{version.manifest_checksum}</dd></div></dl>
      </DetailDrawer>
      <ul className="diagnostics">{version.gate_findings.map((finding, index) => <li key={`${finding.code}-${index}`}><strong>{finding.severity} · {finding.code}</strong><span>{finding.message}</span></li>)}</ul>
      <details className="rawResult"><summary>{messages.skills.viewInterpretationDiff}</summary><pre>{JSON.stringify(version.interpretation_diff, null, 2)}</pre></details>
      <button className="primaryButton" disabled={!version.gate_passed || version.status !== 'DRAFT'} type="button" onClick={onPublish}>{version.status === 'PUBLISHED' ? messages.skills.published : messages.skills.publishVersion}</button>
      {!version.gate_passed && <p className="hint">{messages.skills.hardGateHint}</p>}
    </section>
  )
}

/** Skill parser response から安全境界と draft identity を要約する。 */
export function SkillParseSummary({ result }: { result: SkillParseResult }) {
  const messages = useMessages()
  const normalized = result.normalized_package
  const manifest = result.runtime_manifest_draft
  // Package 診断は manifest compatibility 側へ複製されるため、単純結合すると同一診断が二重表示される。
  const diagnostics = dedupeDiagnostics([...normalized.diagnostics, ...manifest.compatibility.diagnostics])
  return (
    <div className="skillSummary">
      <dl className="runFacts">
        <div><dt>{messages.skills.parseNameLabel}</dt><dd>{normalized.metadata.name}</dd></div>
      </dl>
      <details className="technicalResultDetails">
        <summary>{messages.skills.technicalDetails}</summary>
        <dl className="runFacts">
        <div><dt>{messages.skills.parseAdapterLabel}</dt><dd>{normalized.source.detected_adapter}</dd></div>
        <div><dt>{messages.skills.parseSkillKeyLabel}</dt><dd className="mono">{manifest.identity.skill_key}</dd></div>
        <div><dt>{messages.skills.parseConfidenceLabel}</dt><dd>{manifest.compatibility.confidence}</dd></div>
        <div><dt>{messages.skills.parseToolsLabel}</dt><dd>{manifest.tools.length === 0 ? messages.skills.toolsUnauthorized : manifest.tools.length}</dd></div>
        </dl>
        {normalized.declared_tools.length > 0 && <p className="hint">{messages.skills.declaredToolsLine(normalized.declared_tools.join(', '))}</p>}
        {diagnostics.length > 0 && <ul className="diagnostics">{diagnostics.map((diagnostic, index) => <li key={`${diagnostic.code}-${index}`}><strong>{diagnostic.code}</strong><span>{diagnostic.message}</span></li>)}</ul>}
      </details>
      <div className="resourceGrid">
        <Metric label={messages.skills.metricFiles} value={normalized.source.files.length} />
        <Metric label={messages.skills.metricReferences} value={normalized.resources.references.length} />
        <Metric label={messages.skills.metricScripts} value={normalized.resources.scripts.length} />
        <Metric label={messages.skills.metricAssets} value={normalized.resources.assets.length} />
      </div>
    </div>
  )
}

/** 保存済み source と interpretation の不変 identity を表示する。 */
function SavedSkillIdentity({ stored }: { stored: StoredSkillPreviewRecord }) {
  const messages = useMessages()
  return (
    <div className="savedSkill">
      <div className="savedSkillStatus"><span>{messages.skills.savedInterpretationStatus}</span><strong>{stored.interpretation_status}</strong></div>
      <DetailDrawer title={messages.elements.technicalDetails}>
        <dl className="runFacts">
          <div><dt>{messages.skills.savedSourceId}</dt><dd className="mono">{stored.skill_source_id}</dd></div>
          <div><dt>{messages.skills.savedInterpretationId}</dt><dd className="mono">{stored.interpretation_id}</dd></div>
        </dl>
      </DetailDrawer>
    </div>
  )
}

/** Small numeric metric を parser summary で揃えて表示する。 */
function Metric({ label, value }: { label: string; value: number }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>
}

/** 同一内容の診断を code・message・位置で一意化する。severity は同一 code 内で変わらない前提。 */
function dedupeDiagnostics(items: SkillDiagnostic[]): SkillDiagnostic[] {
  const seen = new Set<string>()
  return items.filter((item) => {
    const key = [item.code, item.message, item.path ?? '', item.line ?? ''].join('\u0000')
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

/** Revision diff を dimension 別の短い行へ要約する。 */
function summarizeDiff(messages: UiMessages, diff: Record<string, unknown>): string[] {
  const lines: string[] = []
  for (const dimension of ['capabilities', 'tasks', 'data_sources', 'tools', 'workflows']) {
    const value = diff[dimension]
    if (!isPlainRecord(value)) continue
    const added = countArray(value.added)
    const removed = countArray(value.removed)
    const changed = countArray(value.changed)
    if (added + removed + changed > 0) lines.push(`${dimension}: +${added} / -${removed} / ~${changed}`)
  }
  const level = diff.compatibility_level
  if (isPlainRecord(level)) lines.push(`compatibility_level: ${String(level.from)} → ${String(level.to)}`)
  for (const dimension of ['identity', 'permissions', 'ui', 'confidence']) {
    const value = diff[dimension]
    if (isPlainRecord(value) && isPlainRecord(value.changed)) {
      const count = Object.keys(value.changed).length
      if (count > 0) lines.push(`${dimension}: ${messages.skills.changedCount(count)}`)
    }
  }
  const diagnostics = diff.diagnostics
  if (isPlainRecord(diagnostics)) {
    const added = countArray(diagnostics.added)
    const removed = countArray(diagnostics.removed)
    if (added + removed > 0) lines.push(`diagnostics: +${added} / -${removed}`)
  }
  return lines
}

/** Adjustment record から人が読める instruction を安全に取り出す。 */
function readAdjustmentInstruction(adjustment: Record<string, unknown> | null): string | null {
  if (adjustment === null) return null
  const value = adjustment.instruction
  return typeof value === 'string' ? value : null
}

/** Object を厳密に判定する（配列や null を除く）。 */
function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 配列なら長さを、そうでなければ 0 を返す。 */
function countArray(value: unknown): number {
  return Array.isArray(value) ? value.length : 0
}
