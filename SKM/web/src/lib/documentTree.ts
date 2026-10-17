import type { ProjectDocumentRecord } from '../api'

/** folder path を実際の階層として表示するための文書 tree node。root は name/path とも空文字。 */
export interface DocumentTreeNode {
  name: string
  path: string
  folders: DocumentTreeNode[]
  files: ProjectDocumentRecord[]
}

/** 文書一覧から folder path の実階層 tree を構築する。folder・file とも名称昇順で安定させる。 */
export function buildDocumentTree(documents: ProjectDocumentRecord[], emptyFolders: string[] = [], sort = 'name'): DocumentTreeNode {
  const root: DocumentTreeNode = { name: '', path: '', folders: [], files: [] }
  const nodes = new Map<string, DocumentTreeNode>([['', root]])
  const ensureFolder = (path: string): DocumentTreeNode => {
    const existing = nodes.get(path)
    if (existing) return existing
    const segments = path.split('/')
    const parent = ensureFolder(segments.slice(0, -1).join('/'))
    const node: DocumentTreeNode = { name: segments.at(-1) ?? '', path, folders: [], files: [] }
    parent.folders.push(node)
    nodes.set(path, node)
    return node
  }
  for (const path of emptyFolders) ensureFolder(path)
  for (const document of documents) {
    ensureFolder(document.folder).files.push(document)
  }
  const sortNode = (node: DocumentTreeNode): void => {
    node.folders.sort((left, right) => left.name.localeCompare(right.name))
    node.files.sort((left, right) => (sort === 'size' ? right.size - left.size : sort === 'date' ? Date.parse(right.created_at) - Date.parse(left.created_at) : 0) || left.name.localeCompare(right.name))
    node.folders.forEach(sortNode)
  }
  sortNode(root)
  return root
}

/** Folder 配下(子孫含む)の文書件数を数える。 */
export function countDocuments(node: DocumentTreeNode): number {
  return node.files.length + node.folders.reduce((sum, child) => sum + countDocuments(child), 0)
}
