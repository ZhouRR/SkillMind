import { projectDocumentContentHref, type ProjectDocumentRecord } from '../api'
import { useMessages } from '../i18n'
import { DOCUMENT_PREVIEW_MAX_BYTES as PREVIEW_MAX_BYTES, documentPreviewKind, type DocumentPreviewKind } from '../lib/documentPreview'
import { countDocuments, type DocumentTreeNode } from '../lib/documentTree'
import { documentTypeLabel, formatByteSize, formatLocalTimestamp } from '../lib/presentation'
import { ActionMenu, type ActionMenuItem } from './ActionMenu'

/** 文書 tree の選択と upload 先を親 owner へ返す操作。 */
export interface DocumentTreeSelection {
  selectedIds: ReadonlySet<string>
  disabled: boolean
  toggle: (id: string, checked: boolean) => void
  onFolder: (path: string) => void
  edit?: (document: ProjectDocumentRecord) => void
  replace?: (document: ProjectDocumentRecord) => void
  folderEdit?: (path: string) => void
  folderDelete?: (path: string) => void
  folderRecycle?: (path: string) => void
  folderSelect?: (path: string, checked: boolean) => void
  purge?: (document: ProjectDocumentRecord) => void
  trashed?: boolean
}

/** 文書一覧を folder path の実階層で表示する presentational tree。folder 先行・file 後続で安定表示する。 */
export function DocumentTree({ root, projectId, busyId, onDelete, onPreview, selection, expandFolders = false }: {
  root: DocumentTreeNode
  expandFolders?: boolean
  projectId: string
  busyId: string | null
  selection?: DocumentTreeSelection
  onDelete: (document: ProjectDocumentRecord) => void
  onPreview: (document: ProjectDocumentRecord, kind: DocumentPreviewKind) => void
}) {
  return (
    <div className="documentTree">
      {root.folders.map((folder) => (
        <FolderNode
          key={folder.path}
          folder={folder}
          expandFolders={expandFolders}
          projectId={projectId}
          busyId={busyId}
          selection={selection}
          onDelete={onDelete}
          onPreview={onPreview}
        />
      ))}
      {root.files.length > 0 && (
        <ul className="documentList">
          {root.files.map((document) => (
            <FileRow
              key={document.document_id}
              document={document}
              projectId={projectId}
              busyId={busyId}
              selection={selection}
              onDelete={onDelete}
              onPreview={onPreview}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

/** 一つの folder を開閉可能な節として描画し、子 folder → file の順で内容を並べる。 */
function FolderNode({ folder, projectId, busyId, onDelete, onPreview, selection, expandFolders }: {
  folder: DocumentTreeNode
  expandFolders: boolean
  projectId: string
  busyId: string | null
  selection?: DocumentTreeSelection
  onDelete: (document: ProjectDocumentRecord) => void
  onPreview: (document: ProjectDocumentRecord, kind: DocumentPreviewKind) => void
}) {
  const messages = useMessages()
  const total = countDocuments(folder)
  const selected = selection ? countSelectedDocuments(folder, selection.selectedIds) : 0
  const actions: ActionMenuItem[] = []
  if (selection && !selection.trashed) actions.push({ id: 'upload', label: messages.documentsPanel.uploadHere,
    disabled: selection.disabled, onSelect: () => selection.onFolder(folder.path) })
  if (selection?.folderEdit) actions.push({ id: 'organize', label: `${messages.fileManagement.rename} / ${messages.fileManagement.move}`,
    disabled: selection.disabled, onSelect: () => selection.folderEdit?.(folder.path) })
  if (total > 0 && selection?.folderRecycle) actions.push({ id: 'recycle',
    label: selection.trashed ? messages.fileManagement.restore : messages.fileManagement.trashAction,
    disabled: selection.disabled, danger: !selection.trashed, separatorBefore: true,
    onSelect: () => selection.folderRecycle?.(folder.path) })
  if (total === 0 && selection?.folderDelete) actions.push({ id: 'delete', label: messages.fileManagement.emptyFolder,
    disabled: selection.disabled, danger: true, separatorBefore: true, onSelect: () => selection.folderDelete?.(folder.path) })
  return (
    <details className="docFolder" open={expandFolders}>
      <summary>
        {selection?.folderSelect && <input type="checkbox" checked={total > 0 && selected === total}
          disabled={selection.disabled || total === 0}
          aria-label={`${messages.fileManagement.selectFolder}: ${folder.name}`}
          ref={(element) => { if (element) element.indeterminate = selected > 0 && selected < total }}
          onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}
          onChange={(event) => selection.folderSelect?.(folder.path, event.target.checked)} />}
        <svg className="docFolderIcon" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M1.8 4.2a1 1 0 0 1 1-1h3.4l1.6 1.8h5.4a1 1 0 0 1 1 1v6.2a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1Z" />
        </svg>
        <strong title={folder.path}>{folder.name}</strong>
        {/* 数字だけだと何の数か分からないため、配下の文書数として単位を付ける。 */}
        <span className="docFolderCount">{messages.workspace.documentSelection.memberCount(total)}</span>
        <ActionMenu id={`document-folder-actions-${projectId}-${encodeURIComponent(folder.path)}`}
          label={messages.common.moreActions(folder.name)} ownerKey={folder.path} items={actions} />
      </summary>
      <div className="docFolderBody">
        <details className="documentFolderPath detailDisclosure"><summary>{messages.assetsAudit.fullPath}</summary><code>{folder.path}</code></details>
        {folder.folders.map((child) => (
          <FolderNode
            key={child.path}
            folder={child}
            expandFolders={expandFolders}
            projectId={projectId}
            busyId={busyId}
            selection={selection}
            onDelete={onDelete}
            onPreview={onPreview}
          />
        ))}
        {folder.files.length > 0 && (
          <ul className="documentList">
            {folder.files.map((document) => (
              <FileRow
                key={document.document_id}
                document={document}
                projectId={projectId}
                busyId={busyId}
                selection={selection}
                onDelete={onDelete}
                onPreview={onPreview}
              />
            ))}
          </ul>
        )}
      </div>
    </details>
  )
}

/** 検索で絞った tree 内だけを数え、非表示の文書を一括操作へ混入させない。 */
function countSelectedDocuments(folder: DocumentTreeNode, selectedIds: ReadonlySet<string>): number {
  return folder.files.filter((document) => selectedIds.has(document.document_id)).length
    + folder.folders.reduce((sum, child) => sum + countSelectedDocuments(child, selectedIds), 0)
}

/** 一つの文書 row。preview は登録拡張子かつ上限内のときだけ有効化する。 */
function FileRow({ document, projectId, busyId, onDelete, onPreview, selection }: {
  document: ProjectDocumentRecord
  projectId: string
  busyId: string | null
  selection?: DocumentTreeSelection
  onDelete: (document: ProjectDocumentRecord) => void
  onPreview: (document: ProjectDocumentRecord, kind: DocumentPreviewKind) => void
}) {
  const messages = useMessages()
  const kind = documentPreviewKind(document.name)
  const oversized = document.size > PREVIEW_MAX_BYTES
  const downloadHref = projectDocumentContentHref(projectId, document.document_id)
  const typeLabel = documentTypeLabel(document.name)
  const actions: ActionMenuItem[] = [{ id: 'download', label: messages.documentsPanel.download, href: downloadHref, download: document.name }]
  if (selection?.replace) actions.push({ id: 'update', label: messages.documentsPanel.updateFile,
    disabled: selection.disabled, onSelect: () => selection.replace?.(document) })
  if (selection?.edit) actions.push({ id: 'organize', label: `${messages.fileManagement.rename} / ${messages.fileManagement.move}`,
    disabled: selection.disabled, onSelect: () => selection.edit?.(document) })
  actions.push({ id: 'recycle', label: busyId === document.document_id ? messages.documentsPanel.deleting
    : selection?.trashed ? messages.fileManagement.restore : messages.fileManagement.trashAction,
    disabled: busyId !== null || selection?.disabled, danger: !selection?.trashed, separatorBefore: true,
    onSelect: () => onDelete(document) })
  if (selection?.purge) actions.push({ id: 'purge', label: messages.fileManagement.purge,
    disabled: selection.disabled, danger: true, onSelect: () => selection.purge?.(document) })
  return (
    <li className="documentItem">
      {selection && <input type="checkbox" checked={selection.selectedIds.has(document.document_id)} disabled={selection.disabled}
        aria-label={messages.documentsPanel.selectFile(document.name)}
        onChange={(event) => selection.toggle(document.document_id, event.target.checked)} />}
      <div className="documentInfo">
        {kind !== null && !oversized ? <button className="documentName" type="button" title={document.name}
          aria-label={`${messages.documentsPanel.previewButton}: ${document.name}`}
          onClick={() => onPreview(document, kind)}>{document.name}</button>
          : <a className="documentName" href={downloadHref} download={document.name}
            aria-label={`${messages.documentsPanel.download}: ${document.name}`}
            title={oversized ? messages.documentsPanel.oversizedTitle : document.name}>{document.name}</a>}
        <span title={document.mime}>
          {formatByteSize(document.size)}{typeLabel ? ` · ${typeLabel}` : ''}
          {' · '}{formatLocalTimestamp(document.created_at)}
          <span className="documentPreviewAvailability">{kind !== null && !oversized ? messages.assetsAudit.previewAvailable : oversized ? messages.assetsAudit.downloadOversized : messages.assetsAudit.downloadUnsupported}</span>
        </span>
      </div>
      <div className="documentActions">
        <ActionMenu id={`document-actions-${projectId}-${document.document_id}`}
          label={messages.common.moreActions(document.name)} ownerKey={document.document_id} items={actions} />
      </div>
    </li>
  )
}

