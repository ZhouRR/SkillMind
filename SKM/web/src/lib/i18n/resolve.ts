import { UI_LANGUAGES, type UiLanguage } from './messages'

/** 保存済み preference と browser 言語から表示言語を決定する。

    優先順:有効な保存値 → browser 言語の前方一致(zh-CN → zh など)→ 既定 `zh`。
    docs/01 §17 の決定に従い、未対応言語は error にせず既定へ畳む。 */
export function resolveUiLanguage(
  saved: string | null | undefined,
  browserLanguages: readonly string[],
): UiLanguage {
  const fromSaved = asUiLanguage(saved)
  if (fromSaved) return fromSaved
  for (const candidate of browserLanguages) {
    const matched = asUiLanguage(candidate.toLowerCase().split('-', 1)[0])
    if (matched) return matched
  }
  return 'zh'
}

/** Unknown 値を許可集合内の UiLanguage へ絞り、集合外は null を返す。 */
export function asUiLanguage(value: string | null | undefined): UiLanguage | null {
  return UI_LANGUAGES.find((language) => language === value) ?? null
}
