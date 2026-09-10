import { asUiLanguage } from '../lib/i18n/resolve'
import type { UiLanguage } from '../lib/i18n/messages'
import { API_BASE, isRecord, requestApiJson } from './http'

/** User account に保存された nullable Project preference。 */
export interface ProjectPreferenceRecord {
  project_id: string | null
}

/** 現在も利用可能な保存済み Project preference を取得する。 */
export async function loadProjectPreference(signal?: AbortSignal): Promise<ProjectPreferenceRecord> {
  return parsePreference(await requestApiJson(
    `${API_BASE}/users/me/project-preference`,
    { signal },
  ))
}

/** CSRF token を使って User 自身の Project preference を更新する。 */
export async function saveProjectPreference(
  projectId: string | null,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectPreferenceRecord> {
  return parsePreference(await requestApiJson(
    `${API_BASE}/users/me/project-preference`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ project_id: projectId }),
      signal,
    },
  ))
}

/** Unknown response を nullable UUID preference contract へ制限する。 */
function parsePreference(value: unknown): ProjectPreferenceRecord {
  if (!isRecord(value)
    || !Object.hasOwn(value, 'project_id')
    || (value.project_id !== null && typeof value.project_id !== 'string')) {
    throw new Error('Project preference response did not match its contract')
  }
  return { project_id: value.project_id }
}

/** 保存済み UI 言語 preference を取得する。null は未設定(browser 追従)。 */
export async function loadUiLanguage(signal?: AbortSignal): Promise<UiLanguage | null> {
  return parseUiLanguage(await requestApiJson(
    `${API_BASE}/users/me/ui-language`,
    { signal },
  ))
}

/** CSRF token を使って User 自身の UI 言語 preference を更新する。 */
export async function saveUiLanguage(
  uiLanguage: UiLanguage | null,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<UiLanguage | null> {
  return parseUiLanguage(await requestApiJson(
    `${API_BASE}/users/me/ui-language`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ ui_language: uiLanguage }),
      signal,
    },
  ))
}

/** Unknown response を許可集合内の nullable UI 言語 contract へ制限する。 */
function parseUiLanguage(value: unknown): UiLanguage | null {
  if (!isRecord(value) || !Object.hasOwn(value, 'ui_language')) {
    throw new Error('UI language preference response did not match its contract')
  }
  if (value.ui_language === null) return null
  const language = typeof value.ui_language === 'string' ? asUiLanguage(value.ui_language) : null
  if (language === null) {
    throw new Error('UI language preference response did not match its contract')
  }
  return language
}
