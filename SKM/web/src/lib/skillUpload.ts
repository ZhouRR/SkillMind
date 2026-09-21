/** Browser directory 選択が付ける共通 root 名を除いた Skill 内相対 path を返す。 */
export function normalizedSkillUploadPaths(
  files: readonly Pick<File, 'name' | 'webkitRelativePath'>[],
): string[] {
  const paths = files.map((file) => file.webkitRelativePath || file.name)
  if (paths.includes('SKILL.md')) return paths
  const segments = paths.map((path) => path.split('/').filter(Boolean))
  const commonRoot = segments[0]?.[0]
  if (
    commonRoot === undefined
    || segments.some((parts) => parts.length < 2 || parts[0] !== commonRoot)
  ) return paths
  const stripped = segments.map((parts) => parts.slice(1).join('/'))
  return stripped.includes('SKILL.md') ? stripped : paths
}

/** 上传目录の読取専用 preview entry。text は内容を持ち、binary/過大は種別だけ示す。 */
export interface UploadedSourceFile {
  path: string
  size: number
  kind: 'text' | 'binary' | 'oversized'
  content?: string
}

/** 拡張子で text 判定する許可リスト。mime が text/* の file はこの表になくても text 扱いにする。 */
const TEXT_PREVIEW_EXTENSIONS = new Set([
  'md', 'markdown', 'txt', 'json', 'yaml', 'yml', 'toml', 'csv', 'xml',
  'html', 'htm', 'css', 'js', 'jsx', 'ts', 'tsx', 'py', 'sh',
])

/** 1 file あたりの text preview 上限。超過は内容を読まず oversized として扱う。 */
const TEXT_PREVIEW_MAX_BYTES = 262_144

/** 上传 file 群を preview entry へ変換する。SKILL.md を先頭に固定し、残りは path 昇順。 */
export async function readUploadedSourcePreview(
  files: readonly File[],
): Promise<UploadedSourceFile[]> {
  const paths = normalizedSkillUploadPaths(files)
  const entries = await Promise.all(files.map(async (file, index): Promise<UploadedSourceFile> => {
    const path = paths[index] ?? file.name
    const extension = path.slice(path.lastIndexOf('.') + 1).toLowerCase()
    const isText = file.type.startsWith('text/') || TEXT_PREVIEW_EXTENSIONS.has(extension)
    if (!isText) return { path, size: file.size, kind: 'binary' }
    if (file.size > TEXT_PREVIEW_MAX_BYTES) return { path, size: file.size, kind: 'oversized' }
    return { path, size: file.size, kind: 'text', content: await file.text() }
  }))
  return entries.sort((left, right) => {
    const leftRank = left.path.endsWith('SKILL.md') ? 0 : 1
    const rightRank = right.path.endsWith('SKILL.md') ? 0 : 1
    return leftRank !== rightRank ? leftRank - rightRank : left.path.localeCompare(right.path)
  })
}
