import { ApiProblemError } from '../api/http'
import type { UiMessages } from './i18n/messages'

/** 共通の権限拒否を安定 code で翻訳し、業務固有の失敗分類は変更しない。 */
export function apiErrorMessage(error: unknown, fallback: string, messages: UiMessages): string {
  if (error instanceof ApiProblemError && error.status === 403 && error.code === 'administrator_required') {
    return messages.account.failures.adminRequired
  }
  return error instanceof Error ? error.message : fallback
}
