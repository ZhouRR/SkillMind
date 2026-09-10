import { useSyncExternalStore } from 'react'

/** Browser の履歴と明示選択の双方を、同じ外部 store として購読する。 */
function subscribe(changed: () => void): () => void {
  window.addEventListener('hashchange', changed)
  window.addEventListener('popstate', changed)
  return () => {
    window.removeEventListener('hashchange', changed)
    window.removeEventListener('popstate', changed)
  }
}

/** URL の Project/Run/Task 変更も描画へ反映し、route 名だけで旧対象を残さない。 */
export function useHashRoute(): string {
  return useSyncExternalStore(subscribe, () => window.location.hash, () => '#/')
}

/** 選択直後に通知して旧画面を隔離する。pushState だけでは hashchange は起きない。 */
export function navigateHash(hash: string): void {
  if (window.location.hash === hash) return
  window.history.pushState(null, '', hash)
  window.dispatchEvent(new Event('hashchange'))
}
