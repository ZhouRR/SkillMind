import { useState } from 'react'
import { useMessages } from '../i18n'
import { readCronPreset, writeCronPreset } from '../lib/cronPreset'

/** 既存 cron 入力に毎日/毎週の入口を添える。表示の切替だけで原規則は変更しない。 */
export function ScheduleRecurrenceField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const labels = useMessages().schedules
  const [advanced, setAdvanced] = useState(false)
  const preset = readCronPreset(value)
  const mode = advanced || !preset ? 'custom' : preset.mode
  return <div className="scheduleRecurrence">
    <label>{labels.recurrenceLabel}<select name="recurrence" value={mode} onChange={(event) => {
      if (event.target.value === 'custom') { setAdvanced(true); return }
      setAdvanced(false)
      onChange(writeCronPreset({ time: preset?.time ?? '09:00', weekday: preset?.weekday ?? '1', mode: event.target.value === 'weekly' ? 'weekly' : 'daily' }))
    }}>
      {(['daily', 'weekly', 'custom'] as const).map((key) => <option key={key} value={key}>{labels.recurrenceModes[key]}</option>)}
    </select></label>
    {mode === 'custom' ? <label>{labels.cronLabel}<input name="cron_expression" type="text" maxLength={128} value={value} onChange={(event) => onChange(event.target.value)} />
      <span className="hint">{labels.cronHint}</span></label>
      : preset && <div className="cronPresetFields">
        <label>{labels.timeLabel}<input type="time" name="recurrence_time" required value={preset.time} onChange={(event) => {
          if (/^\d{2}:\d{2}$/.test(event.target.value)) onChange(writeCronPreset({ ...preset, time: event.target.value }))
        }} /></label>
        {mode === 'weekly' && <label>{labels.weekdayLabel}<select name="recurrence_weekday" value={preset.weekday}
          onChange={(event) => onChange(writeCronPreset({ ...preset, weekday: event.target.value }))}>
          {labels.weekdays.map((day, index) => <option key={day} value={String(index)}>{day}</option>)}
        </select></label>}
      </div>}
  </div>
}
