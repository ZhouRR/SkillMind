import { useId, useState } from 'react'

import { useMessages } from '../i18n'
import { ALL_DOCUMENTS_SELECTION, MAX_SELECTED_DOCUMENTS, readDocumentSelection, validDocumentSelection } from '../lib/documentSelection'
import type { SourceRequirementChoice } from '../lib/taskDraft'

/** 文書は唯一候補でも明示確認する。単一/集合/全集を同じ sources 契約で即時と調度へ渡す。 */
export function DocumentSourceField({ requirement, value, onChange }: {
  requirement: SourceRequirementChoice
  value: string
  onChange: (value: string) => void
}) {
  const messages = useMessages()
  const labels = messages.workspace.documentSelection
  const helpId = useId()
  const [search, setSearch] = useState('')
  const selection = readDocumentSelection(value)
  const documents = requirement.options.flatMap((option) => {
    const parsed = readDocumentSelection(option.value)
    return parsed.mode === 'SINGLE' && parsed.valid ? [{ ...option, id: parsed.ids[0]! }] : []
  })
  const hasAll = requirement.options.some((option) => option.value === ALL_DOCUMENTS_SELECTION)
  const visible = documents.filter((option) => option.label.toLowerCase().includes(search.toLowerCase()))
  const invalid = !validDocumentSelection(value, requirement)
  const selectedDocument = documents.find((option) => option.id.toLowerCase() === selection.ids[0]?.toLowerCase())
  return (
    <fieldset className="documentSourceField" aria-describedby={helpId}>
      <legend>{messages.workspace.resourceKind.document} · {requirement.key}{requirement.required ? '' : messages.workspace.optionalSuffix}</legend>
      <label>{labels.mode}
        <select
          value={selection.mode === 'NONE' ? '' : selection.mode}
          required={requirement.required}
          onChange={(event) => {
            setSearch('')
            onChange(event.target.value === 'SINGLE' ? 'document:'
              : event.target.value === 'SET' ? 'documents:'
                : event.target.value === 'ALL' ? ALL_DOCUMENTS_SELECTION : '')
          }}
        >
          <option value="">{requirement.required ? labels.choose : messages.workspace.notUsed}</option>
          <option value="SINGLE" disabled={documents.length === 0}>{labels.single}</option>
          <option value="SET" disabled={documents.length < 2}>{labels.set}</option>
          <option value="ALL" disabled={!hasAll}>{labels.all}</option>
          {selection.mode === 'INVALID' && <option value="INVALID" disabled>{labels.invalid}</option>}
        </select>
      </label>
      {selection.mode === 'SINGLE' && (
        <label>{labels.single}
          <select required value={selectedDocument?.value ?? value} onChange={(event) => onChange(event.target.value)}>
            <option value="document:">{labels.choose}</option>
            {!selectedDocument && value !== 'document:' && <option value={value} disabled>{labels.invalid}</option>}
            {documents.map((option) => <option key={option.id} value={option.value}>{option.label}</option>)}
          </select>
        </label>
      )}
      {selection.mode === 'SET' && <>
        <label>{labels.search}
          <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} />
        </label>
        <p className="hint">{labels.count(selection.ids.length)} · {labels.setHint}</p>
        <div className="documentChoices">
          {visible.map((option) => {
            const checked = selection.ids.some((id) => id.toLowerCase() === option.id.toLowerCase())
            return (
              <label key={option.id}>
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={!checked && selection.ids.length >= MAX_SELECTED_DOCUMENTS}
                  onChange={() => {
                    const ids = checked ? selection.ids.filter((id) => id.toLowerCase() !== option.id.toLowerCase()) : [...selection.ids, option.id]
                    onChange(`documents:${ids.join(',')}`)
                  }}
                />
                <span>{option.label}</span>
              </label>
            )
          })}
          {visible.length === 0 && <p className="hint">{labels.noMatches}</p>}
        </div>
      </>}
      <p className="hint" id={helpId}>{selection.mode === 'ALL' ? labels.allHint : labels.freezeHint}</p>
      {value !== '' && invalid && <p className="error" role="status">{labels.invalid}</p>}
      {documents.length === 0 && <p className="hint">{messages.workspace.sourceNotConfigured}</p>}
    </fieldset>
  )
}
