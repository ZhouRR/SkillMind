/** Web テスト共通の demo record。src/api の契約型と同じ形状を単一箇所で維持する。 */

import type { AuthenticatedUserRecord, AuthSessionRecord, ProjectRecord } from '../src/api'

/** テスト横断で共有する代表 Project。 */
export const DEMO_PROJECT: ProjectRecord = {
  project_id: '00000000-0000-4000-8000-000000000010',
  key: 'quality-team',
  name: 'Quality Team',
  description: 'Synthetic quality analysis project',
  status: 'ACTIVE',
  settings: {},
  retention_days: 90,
  row_version: 1,
  created_at: '2026-07-05T10:00:00Z',
  updated_at: '2026-07-05T10:00:00Z',
}

/** 指定 system role の認証済み user を生成する。 */
export function demoUser(
  role: AuthenticatedUserRecord['system_role'] = 'USER',
): AuthenticatedUserRecord {
  return {
    user_id: '00000000-0000-4000-8000-000000000001',
    organization_id: '00000000-0000-4000-8000-000000000002',
    email: role === 'ADMIN' ? 'admin@example.com' : 'user@example.com',
    display_name: role === 'ADMIN' ? 'Admin' : 'User',
    system_role: role,
  }
}

/** 指定 system role の認証 session を生成する。 */
export function demoSession(
  role: AuthenticatedUserRecord['system_role'] = 'USER',
): AuthSessionRecord {
  return {
    user: demoUser(role),
    csrf_token: 's'.repeat(32),
    absolute_expires_at: '2026-07-05T12:00:00Z',
  }
}
