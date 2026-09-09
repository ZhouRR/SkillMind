import { userFailure, type UserFailure } from '../lib/userFeedback'
import {
  RESOURCE_REQUEST_TIMEOUT_MS, useResourceMutation, useResourceQuery,
  type ResourceRequestPolicy, type SessionEnded,
} from './useResourceRequest'

export type { SessionEnded } from './useResourceRequest'

/** 既存のアカウント画面も共用 request 境界の待機上限を使う。 */
export const USER_REQUEST_TIMEOUT_MS = RESOURCE_REQUEST_TIMEOUT_MS

/** アカウント固有の拒否分類だけを共通の非同期制御へ渡す。 */
const USER_REQUEST_POLICY: ResourceRequestPolicy<UserFailure> = {
  classify: userFailure,
  readTimeout: { key: 'loadFailed' },
  writeTimeout: { key: 'unknown' },
  blocks: (failure) => failure.key === 'unknown' || failure.key === 'versionConflict',
}

/** 既存のアカウント consumer の公開入口を維持する。 */
export function useUserQuery<T>(key: string, loader: (signal: AbortSignal) => Promise<T>, onSessionEnded: SessionEnded) {
  return useResourceQuery(key, loader, onSessionEnded, USER_REQUEST_POLICY)
}

/** 原版・未知判定を保ち、資源間で防重と期限処理を重複させない。 */
export function useUserMutation(onSessionEnded: SessionEnded) {
  return useResourceMutation(onSessionEnded, USER_REQUEST_POLICY)
}
