import { useId, useState } from 'react'

import { useMessages } from '../i18n'
import { ALL_DOCUMENTS_SELECTION, MAX_SELECTED_DOCUMENTS, documentFolders, readDocumentSelection, sameDocumentMembers, validDocumentSelection } from '../lib/documentSelection'
import type { SourceRequirementChoice } from '../lib/taskDraft'

/** フォルダー選択を明示的な文書 ID へ展開し、即時と調度で同じ sources 契約を使う。 */
export function DocumentSourceField({ requirement, value, onChange }: {
  requirement: SourceRequirementChoice
  value: string
  onChange: (value: string) => void
}) {
  const messages = useMessages()
  const labels = messages.workspace.documentSelection
  const helpId = useId()
  const [chosenFolder, setChosenFolder] = useState<{ path: string; value: string } | null>(null)
  const selection = readDocumentSelection(value)
  const folders = documentFolders(requirement.options)
  // 一件だけのフォルダーは SINGLE で送信するが、操作中の表示はフォルダーのまま保つ。
  const folderChoice = chosenFolder?.value === value ? chosenFolder : null
  const mode = folderChoice ? 'SET' : selection.mode
  const selectedFolder = folders.find((folder) =>
    (!folderChoice || folder.path === folderChoice.path) && sameDocumentMembers(folder.ids, selection.ids),
  )
  const documents = requirement.options.flatMap((option) => {
    const parsed = readDocumentSelection(option.value)
    return parsed.mode === 'SINGLE' && parsed.valid ? [{ ...option, id: parsed.ids[0]! }] : []
  })
  const hasAll = requirement.options.some((option) => option.value === ALL_DOCUMENTS_SELECTION)
  const invalid = !validDocumentSelection(value, requirement)
  const selectedDocument = documents.find((option) => option.id.toLowerCase() === selection.ids[0]?.toLowerCase())
  return (
    <fieldset className="documentSourceField" aria-describedby={helpId}>
      <legend>{messages.workspace.resourceKind.document} · {requirement.key}{requirement.required ? '' : messages.workspace.optionalSuffix}</legend>
      <label>{labels.mode}
        <select
          value={mode === 'NONE' ? '' : mode}
          required={requirement.required}
          onChange={(event) => {
            setChosenFolder(null)
            onChange(event.target.value === 'SINGLE' ? 'document:'
              : event.target.value === 'SET' ? 'documents:'
                : event.target.value === 'ALL' ? ALL_DOCUMENTS_SELECTION : '')
          }}
        >
          <option value="">{requirement.required ? labels.choose : messages.workspace.notUsed}</option>
          <option value="SINGLE" disabled={documents.length === 0}>{labels.single}</option>
          <option value="SET" disabled={folders.length === 0}>{labels.set}</option>
          <option value="ALL" disabled={!hasAll}>{labels.all}</option>
          {selection.mode === 'INVALID' && <option value="INVALID" disabled>{labels.invalid}</option>}
        </select>
      </label>
      {mode === 'SINGLE' && (
        <label>{labels.single}
          <select required value={selectedDocument?.value ?? value} onChange={(event) => onChange(event.target.value)}>
            <option value="document:">{labels.choose}</option>
            {!selectedDocument && value !== 'document:' && <option value={value} disabled>{labels.invalid}</option>}
            {documents.map((option) => <option key={option.id} value={option.value}>{option.label}</option>)}
          </select>
        </label>
      )}
      {mode === 'SET' && <>
        <label>{labels.folder}
          <select
            required
            value={selectedFolder?.path ?? (selection.valid ? '__saved__' : '')}
            onChange={(event) => {
              const folder = folders.find((item) => item.path === event.target.value)
              setChosenFolder(folder ? { path: folder.path, value: folder.value } : null)
              onChange(folder?.value ?? 'documents:')
            }}
          >
            <option value="">{labels.choose}</option>
            {!selectedFolder && selection.valid && <option value="__saved__" disabled>{labels.savedSelection}</option>}
            {folders.map((folder) => <option key={folder.path} value={folder.path} disabled={folder.ids.length > MAX_SELECTED_DOCUMENTS}>
              {folder.path === '/' ? labels.rootFolder : folder.path} · {labels.memberCount(folder.ids.length)}
            </option>)}
          </select>
        </label>
        <p className="hint">{labels.count(selection.ids.length)} · {labels.setHint}</p>
        {!selectedFolder && selection.ids.length > 0 && <ul className="documentChoices">
          {selection.ids.map((id) => <li key={id}>{documents.find((document) => document.id.toLowerCase() === id.toLowerCase())?.label ?? labels.invalid}</li>)}
        </ul>}
      </>}
      <p className="hint" id={helpId}>{selection.mode === 'ALL' ? labels.allHint : labels.freezeHint}</p>
      {value !== '' && invalid && <p className="error" role="status">{labels.invalid}</p>}
      {documents.length === 0 && <p className="hint">{messages.workspace.sourceNotConfigured}</p>}
    </fieldset>
  )
}
