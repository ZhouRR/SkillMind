import { createContext, useContext, type ReactNode } from 'react'

import { MESSAGES, type UiMessages, type UiLanguage } from './lib/i18n/messages'

/** Provider 不在(既存 component test など)でも従来表示を保つため、既定は zh とする。 */
const LanguageContext = createContext<{ language: UiLanguage; messages: UiMessages }>({
  language: 'zh',
  messages: MESSAGES.zh,
})

/** 現在言語の文案 catalog を画面へ供給する application 全体の provider。 */
export function LanguageProvider({ language, children }: {
  language: UiLanguage
  children: ReactNode
}) {
  return (
    <LanguageContext.Provider value={{ language, messages: MESSAGES[language] }}>
      {children}
    </LanguageContext.Provider>
  )
}

/** 現在言語の文案 catalog を返す画面用 hook。 */
export function useMessages(): UiMessages {
  return useContext(LanguageContext).messages
}

/** 現在の言語 code を返す hook。切替 UI の選択状態に使う。 */
export function useUiLanguage(): UiLanguage {
  return useContext(LanguageContext).language
}
