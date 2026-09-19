import { useState } from 'react'
import type { ProjectDocumentRecord } from '../api'
import { useMessages } from '../i18n'
import { ModalDialog } from './PageElements'

export interface DocumentEdit {
  mode: 'CREATE_FOLDER' | 'MOVE' | 'MOVE_FOLDER'
  documents: ProjectDocumentRecord[]
  source?: string
  folder: string
}

/** 同じ文書 ID の表示 path だけを変更する共通 form。 */
export function DocumentOrganizeDialog({ edit, folders, busy, error, onClose, onSave }: {
  edit: DocumentEdit; folders: string[]; busy: boolean; error?: string | null; onClose: () => void
  onSave: (folder: string, name: string) => void
}) {
  const m = useMessages().fileManagement
  const [folder, setFolder] = useState(edit.mode === 'MOVE_FOLDER' ? edit.source ?? '' : edit.folder)
  const [name, setName] = useState(edit.documents.length === 1 ? edit.documents[0]?.name ?? '' : '')
  const title = edit.mode === 'CREATE_FOLDER' ? m.newFolder : edit.mode === 'MOVE_FOLDER' ? `${m.rename} / ${m.move}` : edit.documents.length === 1 ? `${m.rename} / ${m.move}` : m.moveSelected
  return <ModalDialog open title={title} onClose={() => { if (!busy) onClose() }}>
    <form className="formStack" onSubmit={(event) => { event.preventDefault(); if (!busy) onSave(folder, name) }}>
      {error && <p role="alert" className="error">{error}</p>}
      <label>{m.folder}<input autoFocus value={folder} maxLength={200} disabled={busy} list="organize-folders" required={edit.mode !== 'MOVE'} onChange={(e) => setFolder(e.target.value)} /></label>
      <datalist id="organize-folders">{folders.map((path) => <option key={path} value={path} />)}</datalist>
      {edit.mode === 'MOVE' && edit.documents.length === 1 && <label>{m.name}<input value={name} maxLength={200} required disabled={busy} onChange={(e) => setName(e.target.value)} /></label>}
      <div className="formRow"><button className="primaryButton" disabled={busy} type="submit">{m.save}</button><button className="secondaryButton" disabled={busy} type="button" onClick={onClose}>{m.cancel}</button></div>
    </form>
  </ModalDialog>
}
