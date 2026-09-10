import type { RunDocumentSnapshotRecord } from '../api'
import { useMessages } from '../i18n'

/** 凍結した入力の範囲を、分析結果と現在の文書一覧から独立して提示する。 */
export function RunDocumentSnapshots({ snapshots }: { snapshots: readonly RunDocumentSnapshotRecord[] }) {
  const messages = useMessages()
  const labels = messages.runResult.documents
  const modes = messages.workspace.documentSelection
  if (snapshots.length === 0) return null
  return (
    <section className="resultSection frozenDocuments" aria-label={labels.title}>
      <h3>{labels.title}</h3>
      <p className="hint">{labels.hint}</p>
      {snapshots.map((entry) => (
        <div className="frozenDocumentSlot" key={entry.requirement_key}>
          <div className="subsectionHeader">
            <strong>{entry.requirement_key}</strong>
            <span>{labels.status[entry.status]}</span>
          </div>
          {entry.status === 'FROZEN' ? <>
            <p>{entry.snapshot.selection_mode === 'SINGLE' ? modes.single : entry.snapshot.selection_mode === 'SET' ? modes.set : modes.all}</p>
            <dl className="frozenDocumentFacts">
              <div><dt>{labels.checksum}</dt><dd><code>{entry.snapshot.checksum}</code></dd></div>
            </dl>
            <details>
              <summary>{labels.members(entry.snapshot.documents.length)}</summary>
              <ul className="frozenDocumentMembers">
                {entry.snapshot.documents.map((document) => (
                  <li key={document.document_id}>
                    <strong>{document.folder ? `${document.folder}/${document.name}` : document.name}</strong>
                    <p className="hint">{document.mime} · {labels.size(document.size)}</p>
                    <dl className="frozenDocumentFacts">
                      <div><dt>{labels.documentId}</dt><dd><code>{document.document_id}</code></dd></div>
                      <div><dt>{labels.contentHash}</dt><dd><code>{document.content_hash}</code></dd></div>
                    </dl>
                  </li>
                ))}
              </ul>
            </details>
          </> : <p className={entry.status === 'INVALID' ? 'error' : 'hint'} role="status">
            {entry.status === 'INVALID' ? labels.invalid : labels.legacy}
          </p>}
        </div>
      ))}
    </section>
  )
}
