import { isResultReferenceChecks, isRunResultValidation, type RunResultDetail } from '../api'
import { useMessages } from '../i18n'

/** 旧 Result や現在の Effect 一覧から、当時の検証範囲を推論して補わない。 */
export function ResultValidationScope({ result, compact = false }: { result: RunResultDetail; compact?: boolean }) {
  const labels = useMessages().runResult.referenceChecks
  const checks = result.validation.reference_checks
  const recorded = isResultReferenceChecks(checks, result.result_kind)
    && isRunResultValidation(result.validation, result.result_kind)
  if (compact) return <p className={`validationBrief${recorded ? '' : ' validationWarning'}`}>
    {recorded ? (checks.version === 'skillmind.result-reference-checks/v2' ? labels.briefV2 : labels.briefV1)
      : Object.hasOwn(result.validation, 'reference_checks') ? labels.invalid : labels.legacy}
  </p>
  return <section className="resultSection resultValidationScope" aria-label={labels.title}>
    <h3>{labels.title}</h3>
    {recorded ? <>
      <p>{labels.recorded}</p>
      <ul>
        <li>{labels.references}</li>
        <li>{checks.effects === 'PLATFORM_RECORD_MATCH' ? labels.effects : labels.effectsNotApplicable}</li>
        <li>{checks.version === 'skillmind.result-reference-checks/v2' ? labels.artifactsVerified : labels.artifacts}</li>
      </ul>
    </> : <p className="hint">{Object.hasOwn(result.validation, 'reference_checks') ? labels.invalid : labels.legacy}</p>}
    <p className="hint">{labels.limit}</p>
  </section>
}
