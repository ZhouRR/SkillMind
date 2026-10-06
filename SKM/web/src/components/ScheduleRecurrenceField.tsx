import { Select } from './Select'
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
    <label>{labels.recurrenceLabel}<Select name="recurrence" value={mode} onValueChange={(nextValue) => {
      if (nextValue === 'custom') { setAdvanced(true); return }
      setAdvanced(false)
      onChange(writeCronPreset({ time: preset?.time ?? '09:00', weekday: preset?.weekday ?? '1', mode: nextValue === 'weekly' ? 'weekly' : 'daily' }))
    }}>
      {(['daily', 'weekly', 'custom'] as const).map((key) => <option key={key} value={key}>{labels.recurrenceModes[key]}</option>)}
    </Select></label>
    {mode === 'custom' ? <label>{labels.cronLabel}<input name="cron_expression" type="text" maxLength={128} value={value} onChange={(event) => onChange(event.target.value)} />
      <span className="hint">{labels.cronHint}</span></label>
      : preset && <div className="cronPresetFields">
        <label>{labels.timeLabel}<input type="time" name="recurrence_time" required value={preset.time} onChange={(event) => {
          if (/^\d{2}:\d{2}$/.test(event.target.value)) onChange(writeCronPreset({ ...preset, time: event.target.value }))
        }} /></label>
        {mode === 'weekly' && <label>{labels.weekdayLabel}<Select name="recurrence_weekday" value={preset.weekday}
          onValueChange={(nextValue) => onChange(writeCronPreset({ ...preset, weekday: nextValue }))}>
          {labels.weekdays.map((day, index) => <option key={day} value={String(index)}>{day}</option>)}
        </Select></label>}
      </div>}
  </div>
}
