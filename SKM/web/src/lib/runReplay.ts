import { RUN_STATUSES, type RunEventRecord, type RunStatus } from '../api'

/** Replay event が現在 snapshot 以上なら適用可能な status/version を返す。 */
export function applicableRunSnapshot(
  event: RunEventRecord,
  currentRowVersion: number,
): { status: RunStatus; rowVersion: number } | null {
  const status = event.payload.status
  const rowVersion = event.payload.row_version
  if (
    event.event_type !== 'RUN_SNAPSHOT'
    || typeof status !== 'string'
    || !RUN_STATUSES.has(status)
    || typeof rowVersion !== 'number'
    || !Number.isInteger(rowVersion)
    || rowVersion < currentRowVersion
  ) {
    return null
  }
  return { status: status as RunStatus, rowVersion }
}
