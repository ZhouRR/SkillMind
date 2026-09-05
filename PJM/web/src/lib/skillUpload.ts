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
