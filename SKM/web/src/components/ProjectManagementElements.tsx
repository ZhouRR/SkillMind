import { useEffect, useRef } from 'react'

import type { ProjectRecord } from '../api'
import { useMessages } from '../i18n'
import type { ProjectFailure } from '../lib/projectFeedback'
import type { ProjectDraft, ProjectIntent } from '../lib/projectManagement'

/** 公開分類だけを表示し、拒否通知に keyboard focus を移す。 */
export function ProjectResponseNotice({ failure }: { failure: ProjectFailure | null }) {
  const messages = useMessages().projectManagement
  const target = useRef<HTMLDivElement>(null)
  useEffect(() => { if (failure) target.current?.focus() }, [failure])
  return failure && <div className="projectResponse error" role="alert" tabIndex={-1} ref={target}>{messages.failures[failure.key]}</div>
}

/** 元 ID/key/版は常に表示し、status と retention を停止/清掃の証明へ読み替えない。 */
export function ProjectFacts({ project }: { project: ProjectRecord }) {
  const messages = useMessages()
  return <div className="projectFacts">
    <strong>{project.name}</strong><p className="mono">{project.project_id}</p><p className="mono">{project.key}</p>
    <dl><div><dt>{messages.projectManagement.version}</dt><dd>{project.row_version}</dd></div>
      <div><dt>{messages.projectManagement.status}</dt><dd>{messages.projectManagement.states[project.status]}</dd></div>
      <div><dt>{messages.projects.descriptionLabel}</dt><dd>{project.description || messages.projects.noDescription}</dd></div>
      <div><dt>{messages.projects.retentionLabel}</dt><dd>{project.retention_days}</dd></div></dl>
  </div>
}

/** 未送信草稿は元 record と分離して表示し、settings は画面で編集しない。 */
export function ProjectDraftFacts({ draft, action }: { draft: ProjectDraft; action: ProjectIntent['action'] }) {
  const messages = useMessages()
  return <div className="projectFacts"><strong>{draft.name}</strong><p className="mono">{draft.key}</p>
    <dl><div><dt>{messages.projectManagement.action}</dt><dd>{messages.projectManagement.actions[action]}</dd></div>
      <div><dt>{messages.projects.descriptionLabel}</dt><dd>{draft.description || messages.projects.noDescription}</dd></div>
      <div><dt>{messages.projects.retentionLabel}</dt><dd>{draft.retentionDays}</dd></div></dl>
    <p className="hint">{messages.projectManagement.settingsPreserved}</p>
  </div>
}

/** 原値・送信草稿・現在値を混ぜず、版の採用と write を別の操作にする。 */
export function ProjectComparison({ intent, current }: { intent: ProjectIntent; current: ProjectRecord | null }) {
  const messages = useMessages().projectManagement
  return <div className="projectComparison" data-project-comparison="">
    <section><h4>{messages.original}</h4>{intent.original ? <ProjectFacts project={intent.original} /> : <p>{messages.newIdentity}</p>}</section>
    <section><h4>{messages.draft}</h4><ProjectDraftFacts draft={intent.draft} action={intent.action} /></section>
    <section><h4>{messages.current}</h4>{current ? <ProjectFacts project={current} /> : <p>{messages.notAccessibleFact}</p>}</section>
  </div>
}

/** 破壊的操作を含め、元対象の確認を行ってから一回だけ submit する。 */
export function ProjectIntentConfirmation({ intent, busy, onConfirm, onCancel }: {
  intent: ProjectIntent; busy: boolean; onConfirm: () => void; onCancel: () => void
}) {
  const messages = useMessages()
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { heading.current?.focus() }, [intent])
  const boundaryHint = intent.action === 'delete' ? messages.projectManagement.deleteHint
    : intent.action === 'archive' ? messages.projectManagement.archiveHint
      : intent.action === 'restore' ? messages.projectManagement.restoreHint : messages.projectManagement.settingsPreserved
  return <section className="panel projectIntent" data-project-intent="">
    <h2 ref={heading} tabIndex={-1}>{messages.projectManagement.confirmTitle}</h2>
    <p>{messages.projectManagement.actions[intent.action]}</p>
    {intent.base && <ProjectFacts project={intent.base} />}
    {(intent.action === 'create' || intent.action === 'edit') && <ProjectDraftFacts draft={intent.draft} action={intent.action} />}
    <p className="hint">{boundaryHint}</p>
    <div className="inlineActions"><button className={intent.action === 'delete' ? 'dangerButton' : 'primaryButton'} type="button" data-project-confirm="" disabled={busy} onClick={onConfirm}>{messages.projectManagement.confirm}</button>
      <button className="secondaryButton" type="button" data-project-cancel="" disabled={busy} onClick={onCancel}>{messages.elements.cancel}</button></div>
  </section>
}
