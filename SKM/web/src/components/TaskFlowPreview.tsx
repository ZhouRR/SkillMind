import type { TaskFlowNote, TaskFlowPreviewRecord, TaskFlowResourceReference, TaskFlowSourceTrace } from '../api'
import { useMessages } from '../i18n'
import { stringifyLosslessJson } from '../lib/losslessJson'
import { TaskReadinessPanel } from './TaskLaunchFields'
import '../styles/taskFlow.css'

/** 元の規則一覧だけを表示し、欠落と明示的な空宣言を区別する。 */
function FlowNotes({ title, notes, referenceBase, traces, ordered = false }: { title: string; notes?: TaskFlowNote[] | null; referenceBase: string; traces: TaskFlowSourceTrace[]; ordered?: boolean }) {
  const labels = useMessages().taskFlow
  const Tag = ordered ? 'ol' : 'ul'
  return <section className="taskFlowSection"><h4>{title}</h4>
    {notes == null ? <p className="hint">{labels.notDeclared}</p>
      : notes.length === 0 ? <p className="hint">{labels.declaredEmpty}</p>
        : <Tag className="taskFlowItems">{notes.map((note, index) => <li key={note.key}>
          <p>{note.text}</p><small className="mono">{note.key}</small>
          <FlowItemSources reference={`${referenceBase}/${index}`} traces={traces} />
        </li>)}</Tag>}
  </section>
}

/** 元 pointer 自身か子孫だけを対応づけ、上位/他 Task の出典を推測で継承しない。 */
function FlowItemSources({ reference, traces }: { reference: string; traces: TaskFlowSourceTrace[] }) {
  const labels = useMessages().taskFlow
  const matching = traces.filter((trace) => trace.target === reference || trace.target.startsWith(`${reference}/`))
  return <details className="taskFlowItemSources" data-flow-item-sources={reference}>
    <summary>{labels.originalReference}: <code>{reference}</code> · {labels.itemSources}</summary>
    {matching.length === 0 ? <p className="hint">{labels.noItemSources}</p> : <FlowTraces traces={matching} />}
  </details>
}

/** 全体と項目別の出典は同じ表示を使い、検査範囲の注意書きを落とさない。 */
function FlowTraces({ traces }: { traces: TaskFlowSourceTrace[] }) {
  const labels = useMessages().taskFlow
  return <ul className="taskFlowItems">{traces.map((trace, index) => <li key={`${trace.target}:${trace.path}:${index}`}>
    <strong className="mono">{trace.path}{trace.line === null ? '' : `:${trace.line}`}</strong>
    <p>{trace.reason}</p><p>{labels.verification[trace.verification]}</p>
    <small>{labels.originalReference}: <code>{trace.target}</code></small>
  </li>)}</ul>
}

/** 宣言された資源だけを示し、候補の選択・binding の変更を行わない。 */
function FlowResources({ title, resources, traces, task = false }: { title: string; resources: TaskFlowResourceReference[]; traces: TaskFlowSourceTrace[]; task?: boolean }) {
  const messages = useMessages()
  const labels = messages.taskFlow
  return <section className="taskFlowSection"><h4>{title}</h4>
    {resources.length === 0 ? <p className="hint">{task ? labels.noTaskResources : labels.declaredEmpty}</p>
      : <ul className="taskFlowItems">{resources.map((item) => <li key={item.value.key}>
        <strong>{messages.workspace.resourceKind[item.value.kind]} · {item.value.required ? messages.workspace.requiredLabel : messages.workspace.optionalLabel}</strong>
        <p><code>{item.value.key}</code> · {labels.access[item.value.access]}</p>
        {item.value.selection_guidance && <p>{item.value.selection_guidance}</p>}
        {item.value.capabilities && <p className="mono">{item.value.capabilities.join(' · ') || labels.declaredEmpty}</p>}
        {item.value.accepted_providers && <p>{labels.providers}: <span className="mono">{item.value.accepted_providers.join(' · ') || labels.declaredEmpty}</span></p>}
        <FlowItemSources reference={item.blueprint_ref} traces={traces} />
      </li>)}</ul>}
  </section>
}

/** 検証済みの単 Task 投影を、実行権限・状態・進捗を追加せず読みやすく分区する。 */
export function TaskFlowPreview({ preview }: { preview: TaskFlowPreviewRecord }) {
  const messages = useMessages()
  const labels = messages.taskFlow
  const plan = preview.plan
  const originalTask = plan?.task.value
  const shared = plan?.shared
  return <div className="taskFlowPreview" data-task-flow-preview data-preview-status={preview.status}>
    <p className="taskFlowNotice">{labels.intro}</p>
    {preview.status === 'NOT_DECLARED' && <p className="hint" data-flow-not-declared>{labels.missing}</p>}
    {plan && originalTask && shared && <>
      <section className="taskFlowScope" data-flow-task>
        <h3>{labels.taskScope}</h3>
        <section className="taskFlowSection"><h4>{labels.objective}</h4><p data-flow-objective>{originalTask.objective}</p>
          <small><code>{originalTask.key}</code> · <code>{originalTask.capability}</code></small>
          <FlowItemSources reference={`${plan.task.blueprint_ref}/objective`} traces={preview.source_traces} />
        </section>
        {originalTask.document_prerequisites && <section className="taskFlowSection">
          <h4>{messages.skills.documentPrerequisites}</h4>
          <p>{originalTask.document_prerequisites.join(' · ')}</p>
          <FlowItemSources reference={`${plan.task.blueprint_ref}/document_prerequisites`} traces={preview.source_traces} />
        </section>}
        <FlowResources title={labels.taskResources} resources={plan.task_resources} traces={preview.source_traces} task />
        <FlowNotes title={labels.success} notes={originalTask.success_criteria} referenceBase={`${plan.task.blueprint_ref}/success_criteria`} traces={preview.source_traces} />
        <section className="taskFlowSection"><h4>{labels.deliverables}</h4><p className="hint">{labels.deliverableHint}</p>
          {originalTask.deliverables === undefined ? <p className="hint">{labels.notDeclared}</p>
            : originalTask.deliverables.length === 0 ? <p className="hint">{labels.declaredEmpty}</p>
              : <ul className="taskFlowItems">{originalTask.deliverables.map((item, index) => <li key={item.key}>
                <strong>{labels.deliverableKinds[item.kind]}</strong><p>{item.description}</p><small className="mono">{item.key}</small>
                <FlowItemSources reference={`${plan.task.blueprint_ref}/deliverables/${index}`} traces={preview.source_traces} />
              </li>)}</ul>}
        </section>
        <details className="taskFlowDetails"><summary>{labels.originalContracts}</summary>
          {(['parameter_contract', 'result_contract'] as const).map((name) => <section key={name}>
            <h4>{name === 'parameter_contract' ? labels.parameters : labels.resultContract}</h4>
            {originalTask[name] === undefined ? <p className="hint">{labels.notDeclared}</p>
              : <pre tabIndex={0}>{stringifyLosslessJson(originalTask[name], 2)}</pre>}
          </section>)}
        </details>
      </section>
      <section className="taskFlowScope" data-flow-shared>
        <h3>{labels.sharedScope}</h3><p className="hint">{labels.sharedHint}</p>
        <FlowResources title={labels.sharedResources} resources={shared.resource_requirements} traces={preview.source_traces} />
        <div className="taskFlowGuidance">
          {(['required_rules', 'recommended_steps', 'quality_criteria', 'prohibited_actions'] as const).map((name) => (
            <FlowNotes key={name} title={labels[name]} notes={shared.guidance?.[name]} ordered={name === 'recommended_steps'} referenceBase={`/guidance/${name}`} traces={preview.source_traces} />
          ))}
        </div>
        <section className="taskFlowSection"><h4>{labels.interactions}</h4><p className="hint">{labels.interactionHint}</p>
          {shared.interaction_points === null ? <p className="hint">{labels.notDeclared}</p>
            : shared.interaction_points.length === 0 ? <p className="hint">{labels.declaredEmpty}</p>
              : <ul className="taskFlowItems">{shared.interaction_points.map((item, index) => <li key={item.key}>
                <strong>{labels.interactionTypes[item.type]}</strong><p>{item.condition}</p>
                {item.prompt && <p>{item.prompt}</p>}<small className="mono">{item.key}</small>
                <FlowItemSources reference={`/interaction_points/${index}`} traces={preview.source_traces} />
              </li>)}</ul>}
        </section>
        <section className="taskFlowSection"><h4>{labels.effects}</h4><p className="hint">{labels.effectHint}</p>
          {shared.effect_intents === null ? <p className="hint">{labels.notDeclared}</p>
            : shared.effect_intents.length === 0 ? <p className="hint">{labels.declaredEmpty}</p>
              : <ul className="taskFlowItems">{shared.effect_intents.map((item, index) => <li key={item.key}>
                <strong>{labels.modes[item.mode]} · {labels.risks[item.risk]}</strong><p>{item.operation}</p>
                {item.resource_key && <p><code>{item.resource_key}</code></p>}
                {item.approval_mode && <p>{labels.approval}</p>}<small className="mono">{item.key}</small>
                <FlowItemSources reference={`/effect_intents/${index}`} traces={preview.source_traces} />
              </li>)}</ul>}
        </section>
        <details className="taskFlowDetails"><summary>{labels.preferences}</summary>
          <p>{labels.profile}: <code>{shared.execution_preferences?.recommended_profile ?? labels.notDeclared}</code></p>
          <FlowNotes title={labels.sessionHints} notes={shared.execution_preferences?.session_split_hints} referenceBase="/execution_preferences/session_split_hints" traces={preview.source_traces} />
          <FlowNotes title={labels.stopConditions} notes={shared.execution_preferences?.stop_conditions} referenceBase="/execution_preferences/stop_conditions" traces={preview.source_traces} />
          <FlowNotes title={labels.assumptions} notes={shared.assumptions} referenceBase="/assumptions" traces={preview.source_traces} />
          <section className="taskFlowSection"><h4>{labels.questions}</h4>
            {shared.questions === null ? <p className="hint">{labels.notDeclared}</p>
              : shared.questions.length === 0 ? <p className="hint">{labels.declaredEmpty}</p>
                : <ul className="taskFlowItems">{shared.questions.map((item, index) => <li key={item.key}>
                  <strong>{item.required ? messages.workspace.requiredLabel : messages.workspace.optionalLabel}</strong><p>{item.text}</p><small className="mono">{item.key}</small>
                  <FlowItemSources reference={`/questions/${index}`} traces={preview.source_traces} />
                </li>)}</ul>}
          </section>
        </details>
      </section>
    </>}
    <section className="taskFlowScope taskFlowReadiness" data-flow-readiness>
      <h3>{labels.readiness}</h3><p className="hint">{labels.readinessHint}</p>
      {preview.readiness.assessment === null ? <p className="hint">{labels.unassessed}</p>
        : <TaskReadinessPanel readiness={preview.readiness.assessment} />}
    </section>
    <details className="taskFlowDetails" data-flow-sources><summary>{labels.sources}</summary>
      <p className="hint">{labels.sourcesHint}</p>
      {preview.source_traces.length === 0 ? <p className="hint">{labels.sourcesEmpty}</p>
        : <FlowTraces traces={preview.source_traces} />}
    </details>
    <details className="taskFlowDetails"><summary>{labels.identity}</summary><dl className="taskFlowIdentity">
      {Object.entries({ ...preview.identity, blueprint_checksum: preview.blueprint_checksum, preview_checksum: preview.preview_checksum }).map(([name, value]) => <div key={name}>
        <dt>{labels.identityLabels[name as keyof typeof labels.identityLabels]}</dt><dd className="mono">{value ?? labels.notDeclared}</dd>
      </div>)}
    </dl></details>
  </div>
}
