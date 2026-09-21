import { useState } from 'react'
import type { ProjectSkillVersionRecord, SkillVersionRecord } from '../api'
import { useMessages } from '../i18n'
import { DetailDrawer, EmptyState, LoadingSkeleton } from './PageElements'
import { ActionMenu } from './ActionMenu'

/** Organization Skill library 一覧の非同期状態。 */
export type SkillLibraryState =
  | { status: 'loading'; versions?: SkillVersionRecord[] }
  | { status: 'ready'; versions: SkillVersionRecord[] }
  | { status: 'error'; message: string; versions?: SkillVersionRecord[] }

/** 選択 Project の精確版有効化一覧の非同期状態。 */
export type ProjectEnablementState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; enablements: ProjectSkillVersionRecord[] }
  | { status: 'error'; message: string }

/** Organization version 一覧と選択 Project の明示有効化関係を同じ精確版単位で表示する。 */
export function SkillLibraryPanel({
  libraryState,
  actionError,
  onRefresh,
  enablementState,
  projectId,
  busyVersionId,
  onPublish,
  onDeprecate,
  onDelete,
  onEnable,
  onDisable,
}: {
  libraryState: SkillLibraryState
  actionError?: { versionId: string; message: string } | null
  onRefresh?: () => void
  enablementState: ProjectEnablementState
  projectId: string
  busyVersionId: string | null
  onPublish: (version: SkillVersionRecord) => void
  onDeprecate: (version: SkillVersionRecord) => void
  onDelete: (version: SkillVersionRecord) => void
  onEnable: (version: SkillVersionRecord) => void
  onDisable: (version: SkillVersionRecord) => void
}) {
  const messages = useMessages()
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const versions = libraryState.versions ?? []
  const enablements = enablementState.status === 'ready' ? enablementState.enablements : []
  const activeIds = new Set(enablements
    .filter(({ disabled_at }) => disabled_at === null)
    .map(({ skill_version }) => skill_version.skill_version_id))
  const disabledIds = new Set(enablements
    .filter(({ disabled_at }) => disabled_at !== null)
    .map(({ skill_version }) => skill_version.skill_version_id))
  const normalizedQuery = query.trim().toLocaleLowerCase()
  const statusOrder = { PUBLISHED: 0, DRAFT: 1, DEPRECATED: 2 }
  // 全件 API の一覧だけを検索する。公開・有効版を先に示し、旧版も同じ一覧で管理できる。
  const visibleVersions = versions.filter((version) => (
    (statusFilter === 'all' || statusFilter === version.status)
    && (!normalizedQuery || `${version.name} ${version.skill_key} ${version.version}`.toLocaleLowerCase().includes(normalizedQuery))
  )).sort((left, right) => (
    Number(activeIds.has(right.skill_version_id)) - Number(activeIds.has(left.skill_version_id))
    || statusOrder[left.status] - statusOrder[right.status]
    || left.name.localeCompare(right.name)
    || right.created_at.localeCompare(left.created_at)
  ))

  function clearFilters(): void {
    setQuery('')
    setStatusFilter('all')
  }

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
      {versions.length > 0 && <div className="skillLibraryFilters">
        <label>{messages.skills.librarySearch}<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        <label>{messages.skills.libraryStatus}<select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
          <option value="all">{messages.skills.libraryAllStatuses}</option>
          {(['PUBLISHED', 'DRAFT', 'DEPRECATED'] as const).map((status) => <option key={status} value={status}>{messages.enums.skillVersionStatus[status]}</option>)}
        </select></label>
        <span className="hint" role="status">{messages.skills.libraryMatches(visibleVersions.length, versions.length)}</span>
        {(query || statusFilter !== 'all') && <button className="secondaryButton" type="button" onClick={clearFilters}>{messages.skills.libraryClearFilters}</button>}
      </div>}
      {enablementState.status === 'error' && <p className="error" role="alert">{enablementState.message}</p>}
      {/* 読み込み中は「空(点線枠)」ではなく骨格行を出す。空態と loading の意味を取り違えさせない。 */}
      {((libraryState.status === 'loading' && versions.length === 0) || enablementState.status === 'loading') && (
        <LoadingSkeleton
          label={libraryState.status === 'loading' ? messages.skills.loadingLibrary : messages.skills.loadingEnablements}
          rows={3}
        />
      )}
      {libraryState.status === 'error' && <div>
        <p className="error" role="alert">{libraryState.message}</p>
        {onRefresh && <button className="secondaryButton" type="button" onClick={onRefresh}>{messages.runHistory.retry}</button>}
      </div>}
      {libraryState.status === 'ready' && libraryState.versions.length === 0 && (
        <EmptyState text={messages.skills.emptyLibrary} />
      )}
      {versions.length > 0 && visibleVersions.length === 0 && <EmptyState text={messages.skills.libraryNoMatches} />}
      {visibleVersions.length > 0 && (
        <ul className="skillLibraryList">
          {visibleVersions.map((version) => {
            const active = activeIds.has(version.skill_version_id)
            const disabled = disabledIds.has(version.skill_version_id)
            const busy = busyVersionId === version.skill_version_id
            const unavailable = busyVersionId !== null || libraryState.status !== 'ready'
            return (
              <li key={version.skill_version_id}>
                {/* 内部 UUID は利用者の判断材料にならないため出さない。読める識別は
                    「名称 + 版 + SKILL.md 原文の説明」で足り、skill_key は追跡用に残す。 */}
                <div className="skillLibraryIdentity">
                  <strong>{version.name} <span className="mono">v{version.version}</span></strong>
                  {version.description && <p className="skillLibraryDescription">{version.description}</p>}
                  <span className="mono">{version.skill_key}</span>
                  {actionError?.versionId === version.skill_version_id && <p className="error" role="alert">{actionError.message}</p>}
                </div>
                <div className="skillLibraryStatus">
                  <span className="statusBadge">{messages.enums.skillVersionStatus[version.status] ?? version.status}</span>
                  {projectId && active && <span className="scopeBadge">{messages.skills.enabledBadge}</span>}
                  {projectId && disabled && <span className="scopeBadge">{messages.skills.disabledBadge}</span>}
                </div>
                <div className="skillActions">
                  {projectId && active && (
                    <button className="secondaryButton" type="button" disabled={unavailable} onClick={() => onDisable(version)}>
                      {busy ? messages.elements.processing : messages.skills.disableFromProject}
                    </button>
                  )}
                  {projectId && !active && !disabled && version.status === 'PUBLISHED' && (
                    <button className="primaryButton" type="button" disabled={unavailable} onClick={() => onEnable(version)}>
                      {busy ? messages.elements.processing : messages.skills.enableForProject}
                    </button>
                  )}
                  {projectId && disabled && (
                    <span className="hint">{messages.skills.disabledAuditHint}</span>
                  )}
                  {/* 廃止しただけでは行が残り続けるため、監査参照のない版に限り片付け経路を出す。
                      参照が残る版は backend が 409 で拒否し、その理由を一覧の error 欄へ出す。 */}
                  <ActionMenu label={messages.common.moreActions(`${version.name} v${version.version}`)} disabled={unavailable} items={[
                    ...(version.status === 'PUBLISHED'
                      ? [{ id: 'deprecate', label: messages.skills.deprecateVersion, onSelect: () => onDeprecate(version) }]
                      : [{ id: 'delete', label: messages.skills.deleteVersion, onSelect: () => onDelete(version), danger: true }]),
                  ]} />
                </div>
                {version.status === 'DRAFT' && (
                  <details className="skillLibraryDraft">
                    <summary>{messages.skills.reviewDraft}</summary>
                    <SkillVersionDetail version={version} disabled={unavailable} onPublish={() => onPublish(version)} />
                  </details>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

/** Frozen Manifest identity、diff、gate finding と publish control を表示する。 */
export function SkillVersionDetail({ version, onPublish, disabled = false }: {
  version: SkillVersionRecord
  onPublish: () => void
  disabled?: boolean
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
      <button className="primaryButton" disabled={disabled || !version.gate_passed || version.status !== 'DRAFT'} type="button" onClick={onPublish}>{version.status === 'PUBLISHED' ? messages.skills.published : messages.skills.publishVersion}</button>
      {!version.gate_passed && <p className="hint">{messages.skills.hardGateHint}</p>}
    </section>
  )
}
