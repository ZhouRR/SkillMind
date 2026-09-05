import type { ProjectRecord } from '../api'

/** 現在 Project を「URL 指定 → 保存済み選択 → 先頭」の順で解決する。

    参照できる Project がある限り必ず一つ選ぶ。未選択のまま置くと、どの画面も
    「先にプロジェクトを選んでください」しか出せず、操作の入口自体が消えるため。

    URL 指定を採用できなかったときも警告は出さない。呼び出し側は解決結果で hash を
    書き戻し、sidebar は選択中 Project を常に名前で示すので、どの Project を見ているかは
    画面上で確定している——失効した link ごとに警告を出しても、利用者が既に見ている事実を
    繰り返すだけになる。 */
export function resolveProjectSelection(
  projects: readonly ProjectRecord[],
  requestedProjectId: string | null,
  preferredProjectId: string | null,
): string {
  const accessible = (candidate: string): boolean =>
    projects.some(({ project_id }) => project_id === candidate)
  if (requestedProjectId !== null && accessible(requestedProjectId)) return requestedProjectId
  const preferred = preferredProjectId !== null && accessible(preferredProjectId)
    ? preferredProjectId
    : null
  return preferred ?? projects[0]?.project_id ?? ''
}
