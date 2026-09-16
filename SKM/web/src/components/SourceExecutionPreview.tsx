import type { SourceExecutionPreview as Preview } from '../api/taskFlowPreview'
import { useMessages } from '../i18n'
import { isRecord } from '../api/http'
import { stringifyLosslessJson } from '../lib/losslessJson'

/** 入力と資源だけを要約し、業務指示は原文として折りたたんで示す。 */
export function SourceExecutionPreview({ preview }: { preview: Preview }) {
  const messages = useMessages()
  const labels = messages.taskFlow
  const task = preview.declaration.tasks[0]
  const fields = Array.isArray(preview.input_contract.fields) ? preview.input_contract.fields : []
  return <div className="taskFlowPreview" data-source-execution-preview>
    <section className="taskFlowSection"><h4>{task.title}</h4><p>{task.description}</p></section>
    <section className="taskFlowSection"><h4>{labels.sourceInputs}</h4>
      {fields.length === 0 ? <p className="hint">{labels.sourceNoInputs}</p>
        : <ul className="taskFlowItems">{fields.filter(isRecord).map((field) => <li key={String(field.key)}>
          <strong>{String(field.key)}</strong> · {field.required ? messages.workspace.requiredLabel : messages.workspace.optionalLabel}
          {typeof field.description === 'string' && <p>{field.description}</p>}
          <details className="taskFlowDetails"><summary>{String(field.type)}</summary>
            <pre tabIndex={0}>{stringifyLosslessJson(field, 2)}</pre>
          </details>
        </li>)}</ul>}
    </section>
    {preview.declaration.resource_requirements.length > 0 && <section className="taskFlowSection"><h4>{labels.sourceResources}</h4>
      <ul className="taskFlowItems">{preview.declaration.resource_requirements.map((resource) => <li key={resource.key}>
        <strong>{messages.workspace.resourceKind[resource.kind]} · {labels.access[resource.access]}</strong>
        <p>{resource.selection_guidance || resource.key}</p>
        <small>{resource.required ? messages.workspace.requiredLabel : messages.workspace.optionalLabel}
          {resource.accepted_providers?.length ? ` · ${resource.accepted_providers.join(' / ')}` : ''}
          {resource.operations.length ? ` · ${resource.operations.map((op) => op.operation).join(' / ')}` : ''}</small>
      </li>)}</ul>
    </section>}
    <section className="taskFlowSection"><h4>{labels.sourceText}</h4>
      {preview.source_documents.map((doc) => <details className="taskFlowDetails" key={doc.path}>
        <summary>{doc.path}</summary><pre tabIndex={0}>{doc.content}</pre>
      </details>)}
    </section>
  </div>
}
