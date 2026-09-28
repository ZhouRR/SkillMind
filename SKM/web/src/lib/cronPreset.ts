/** 固定時刻の毎日/毎週だけを既存 cron と相互変換し、複雑な式は書き換えない。 */
export interface CronPreset { mode: 'daily' | 'weekly'; time: string; weekday: string }

/** 範囲や複数指定は詳細設定に残す。曜日 7 は日曜として表示するが原式は保存まで保持する。 */
export function readCronPreset(expression: string): CronPreset | null {
  const match = /^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+(\*|[0-7])$/.exec(expression.trim())
  if (!match || Number(match[1]) > 59 || Number(match[2]) > 23) return null
  return {
    mode: match[3] === '*' ? 'daily' : 'weekly',
    time: `${match[2]!.padStart(2, '0')}:${match[1]!.padStart(2, '0')}`,
    weekday: match[3] === '*' ? '1' : String(Number(match[3]) % 7),
  }
}

/** User の明示変更だけを五項目式にし、timezone/preview/承認は従来の保存経路へ渡す。 */
export function writeCronPreset(preset: CronPreset): string {
  return `${Number(preset.time.slice(3))} ${Number(preset.time.slice(0, 2))} * * ${preset.mode === 'daily' ? '*' : preset.weekday}`
}
