import { exactFields, isRecord } from './http'

/** 保存時に実施した範囲だけを示し、現在の遠隔状態や添付の可読性は保証しない。 */
export type ResultReferenceChecks = {
  evidence: 'RUN_OWNERSHIP'
  proposals: 'RUN_OWNERSHIP_AND_STATE'
  effects: 'PLATFORM_RECORD_MATCH' | 'NOT_APPLICABLE'
} & ({ version: 'projectmind.result-reference-checks/v1'; artifacts: 'NOT_VERIFIED' }
  | { version: 'projectmind.result-reference-checks/v2'; artifacts: 'RUN_OWNERSHIP_AND_CONTENT' })

/** 歴史の validation は元の field を保持し、新しい範囲を欠損値から補造しない。 */
export interface RunResultValidation extends Record<string, unknown> {
  reference_checks?: ResultReferenceChecks
}

/** 新しい範囲は version と全 field を厳密に読み、Result 種別を越えた主張を拒否する。 */
export function isResultReferenceChecks(
  value: unknown, resultKind: 'OUTCOME_ENVELOPE' | 'STRUCTURED_OUTPUT',
): value is ResultReferenceChecks {
  return isRecord(value)
    && exactFields(value, ['version', 'evidence', 'proposals', 'effects', 'artifacts'])
    && (value.version === 'projectmind.result-reference-checks/v1' && value.artifacts === 'NOT_VERIFIED'
      || value.version === 'projectmind.result-reference-checks/v2' && value.artifacts === 'RUN_OWNERSHIP_AND_CONTENT')
    && value.evidence === 'RUN_OWNERSHIP'
    && value.proposals === 'RUN_OWNERSHIP_AND_STATE'
    && value.effects === (resultKind === 'OUTCOME_ENVELOPE' ? 'PLATFORM_RECORD_MATCH' : 'NOT_APPLICABLE')
}

/** 旧 record はそのまま受け入れるが、存在する不正な新 field を旧形式扱いしない。 */
export function isRunResultValidation(
  value: unknown, resultKind: 'OUTCOME_ENVELOPE' | 'STRUCTURED_OUTPUT',
): value is RunResultValidation {
  if (!isRecord(value)) return false
  if (!Object.hasOwn(value, 'reference_checks')) return true
  return isResultReferenceChecks(value.reference_checks, resultKind)
    && value.schema_valid === true && value.evidence_refs_valid === true
    && value.change_proposal_refs_valid === true
    && (value.reference_checks.version !== 'projectmind.result-reference-checks/v2' || value.artifact_refs_valid === true)
    && (value.reference_checks.version !== 'projectmind.result-reference-checks/v1' || value.artifact_refs_valid !== true)
    && (resultKind !== 'OUTCOME_ENVELOPE' || value.outcome_envelope_valid === true)
}
