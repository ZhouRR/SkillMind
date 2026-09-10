/** 結果一覧を既定で展開する件数。

    結果画面の縦の長さは「一件の高さ」ではなく「件数」で伸びる。成果物・発見・構造化配列は
    Skill 次第で数十件になり得るため、既定は先頭だけを開き、残りは畳んで縦を一定に保つ。 */
export const RESULT_PREVIEW_LIMIT = 5

/** 一覧を「先に見せる分」と「畳む分」へ切る。

    残りが 1 件だけのときは畳まない。開閉行は畳んだ 1 件とほぼ同じ高さを占めるので、
    縦は縮まらないまま操作だけが増える。 */
export function splitOverflow<T>(
  items: readonly T[],
  limit: number = RESULT_PREVIEW_LIMIT,
): { visible: T[]; hidden: T[] } {
  if (items.length <= limit + 1) return { visible: [...items], hidden: [] }
  return { visible: items.slice(0, limit), hidden: items.slice(limit) }
}
