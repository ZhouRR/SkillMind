/** 本文・ID を送信せず、取得と表示の区間をブラウザ Performance API だけへ残す。 */
const received = new WeakMap<object, number>()

/** 観測不能な環境でも API の戻り値と取消を変えない。 */
export function observeRunDetail(value: object, started: number | undefined): void {
  if (started === undefined) return
  try {
    const now = performance.now()
    received.set(value, now)
    performance.clearMeasures('skillmind.run.detail')
    performance.measure('skillmind.run.detail', { start: started, end: now,
      detail: { json_bytes: new TextEncoder().encode(JSON.stringify(value)).byteLength } })
  } catch { /* 観測は業務条件ではない。 */ }
}

/** 取得完了から描画後までを記録し、別 Run/unmount 後は反映しない。 */
export function observeRunPaint(value: object): () => void {
  let frame: number | undefined
  try {
    const start = received.get(value)
    if (start === undefined) return () => {}
    frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => {
        try {
          performance.clearMeasures('skillmind.run.received_to_paint')
          performance.measure('skillmind.run.received_to_paint', { start, end: performance.now() })
        } catch { /* 本文・Run ID・例外は telemetry へ送らない。 */ }
      })
    })
  } catch { /* SSR や計測拒否でも内容表示を継続する。 */ }
  return () => { if (frame !== undefined) cancelAnimationFrame(frame) }
}

/** Performance API が無効でも取得処理を継続する。 */
export function performanceNow(): number | undefined {
  try { return performance.now() } catch { return undefined }
}
