import { API_BASE, hasStrings, isRecord, requestApiJson } from './http'

export const META_ENDPOINT = `${API_BASE}/meta` as const

/** M0 scope と配備経路を Web に通知する API response。 */
export interface ProjectMindMeta {
  name: string
  version: string
  phase: string
  task: string
  ingress: string
}

/** API から ProjectMind metadata を取得する。 */
export async function loadMeta(signal?: AbortSignal): Promise<ProjectMindMeta> {
  const value = await requestApiJson(META_ENDPOINT, { signal })
  if (!isRecord(value) || !hasStrings(value, ['name', 'version', 'phase', 'task', 'ingress'])) {
    throw new Error('API metadata did not match its contract')
  }
  return value as unknown as ProjectMindMeta
}
