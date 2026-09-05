import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  decideChangeProposal,
  loadIntegrations,
  loadSecretReferences,
  type ChangeProposalRecord,
} from '../../src/api/index'

const PROJECT_ID = '00000000-0000-4000-8000-000000000001'
const RUN_ID = '00000000-0000-4000-8000-000000000002'
const CSRF = 'c'.repeat(32)

const PROPOSAL: ChangeProposalRecord = {
  proposal_id: '00000000-0000-4000-8000-000000000003',
  proposal_ref: 'cp_review_001',
  project_id: PROJECT_ID,
  run_id: RUN_ID,
  run_segment_id: '00000000-0000-4000-8000-000000000004',
  agent_session_id: '00000000-0000-4000-8000-000000000005',
  target_binding_id: '00000000-0000-4000-8000-000000000006',
  integration_id: '00000000-0000-4000-8000-000000000007',
  effect_intent_key: 'update_issue',
  capability_version: 'issue.update/v1',
  operation: 'update_fields',
  target: { issue_id: '1001' },
  summary: 'Set the reviewed issue status.',
  changes: [{ op: 'SET', field: 'status_id', value: 3 }],
  precondition: { revision: '17' },
  evidence_refs: ['ev_issue_before_001'],
  risk_level: 'LOW',
  reversible: true,
  rollback: { strategy: 'restore_previous_fields' },
  verification: { mode: 'read_back' },
  status: 'PENDING_APPROVAL',
  version: 4,
  checksum: `sha256:${'a'.repeat(64)}`,
  expires_at: '2026-07-19T00:00:00Z',
  created_at: '2026-07-18T00:00:00Z',
  updated_at: '2026-07-18T00:00:00Z',
}

afterEach(() => vi.unstubAllGlobals())

describe('controlled effect API client', () => {
  it('binds a user decision to the displayed proposal version and checksum', async () => {
    const response = {
      proposal: { ...PROPOSAL, status: 'APPROVED' },
      approval: {
        approval_id: '00000000-0000-4000-8000-000000000008',
        proposal_id: PROPOSAL.proposal_id,
        run_id: RUN_ID,
        source: 'USER',
        decision: 'APPROVED',
        actor_id: '00000000-0000-4000-8000-000000000009',
        preauthorization_id: null,
        proposal_version: PROPOSAL.version,
        proposal_checksum: PROPOSAL.checksum,
        reason: 'Reviewed the exact patch.',
        created_at: '2026-07-18T00:01:00Z',
      },
      effect_execution: null,
      run_status: 'WAITING_FOR_APPROVAL',
      idempotent_replay: false,
    }
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response))
    vi.stubGlobal('fetch', fetchMock)

    await decideChangeProposal(
      PROJECT_ID,
      RUN_ID,
      PROPOSAL,
      'APPROVED',
      'Reviewed the exact patch.',
      'proposal-decision-001',
      CSRF,
    )

    const [, init] = fetchMock.mock.calls[0] ?? []
    expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe(CSRF)
    expect(new Headers(init?.headers).get('Idempotency-Key')).toBe('proposal-decision-001')
    expect(JSON.parse(String(init?.body))).toEqual({
      decision: 'APPROVED',
      proposal_version: 4,
      proposal_checksum: PROPOSAL.checksum,
      reason: 'Reviewed the exact patch.',
    })
  })

  it('rejects SecretReference responses that expose resolver locators', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      items: [{
        secret_reference_id: '00000000-0000-4000-8000-000000000010',
        project_id: PROJECT_ID,
        name: 'Redmine token',
        provider: 'redmine',
        resolver: 'ENVIRONMENT',
        locator: 'REDMINE_API_KEY',
        key_version: 'v1',
        status: 'ACTIVE',
        created_by: '00000000-0000-4000-8000-000000000011',
        created_at: '2026-07-18T00:00:00Z',
        updated_at: '2026-07-18T00:00:00Z',
        disabled_at: null,
      }],
    })))

    await expect(loadSecretReferences(PROJECT_ID)).rejects.toThrow(
      'SecretReference list did not match its contract',
    )
  })

  it('rejects Integration responses that expose connection configuration', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      items: [{
        integration_id: PROPOSAL.integration_id,
        project_id: PROJECT_ID,
        name: 'Redmine',
        kind: 'issue',
        provider: 'redmine',
        status: 'ACTIVE',
        revision: 1,
        capabilities: ['issue.read/v1', 'issue.update/v1'],
        scope: { issue_ids: ['1001'] },
        config: { base_url: 'https://redmine.example.invalid' },
        config_keys: ['base_url'],
        secret_reference_id: null,
        created_by: '00000000-0000-4000-8000-000000000011',
        created_at: '2026-07-18T00:00:00Z',
        updated_at: '2026-07-18T00:00:00Z',
        disabled_at: null,
      }],
    })))

    await expect(loadIntegrations(PROJECT_ID)).rejects.toThrow(
      'Integration list did not match its contract',
    )
  })
})

/** Fetch test 用 JSON response を生成する。 */
function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}
