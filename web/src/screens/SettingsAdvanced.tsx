import { useState } from 'react'
import type { S } from '../api/client'
import { problem, useAdvancedSettings, useSetAdvancedSettings } from '../api/queries'
import { NumberField, Row, Saved, Switch, TextField } from '../components/settings'

/** Settings → Advanced: tuning knobs without a screen of their own, saved as they change. */
export function Advanced() {
  const q = useAdvancedSettings()
  const save = useSetAdvancedSettings()
  const [savedAt, setSavedAt] = useState(0)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const put = (key: string, value: unknown) => {
    setErrors((e) => { const { [key]: _, ...rest } = e; return rest })
    save.mutate({ [key]: value }, { onSuccess: () => setSavedAt(Date.now()), onError: (err) => setErrors((e) => ({ ...e, [key]: problem(err) ?? 'Not saved.' })) })
  }
  const groups: [string, S['AdvancedSetting'][]][] = []
  for (const s of q.data ?? []) {
    const g = groups.find(([name]) => name === s.group)
    if (g) g[1].push(s); else groups.push([s.group, [s]])
  }
  return (
    <>
      <section className="set-section">
        <div className="set-head"><div className="set-title">Advanced</div><Saved token={savedAt} /></div>
        <div className="sub" style={{ fontSize: 13 }}>Fine-tuning. The 1:1 chat can change these for you too.</div>
        {q.isError && <div className="error" role="alert">{problem(q.error)}</div>}
      </section>
      {groups.map(([group, items]) => (
        <section key={group} className="set-section">
          <div className="set-title">{group}</div>
          <div className="set-rows one">
            {items.map((s) => {
              const custom = JSON.stringify(s.value) !== JSON.stringify(s.default)
              const was = custom ? `Default ${s.unit && s.kind !== 'bool' ? `${String(s.default)} ${s.unit}` : String(s.default)}` : undefined
              return (
                <Row key={s.key} label={s.label} hint={<>{s.help}{errors[s.key] && <span role="alert" style={{ display: 'block', color: 'var(--badInk)' }}>{errors[s.key]}</span>}</>}
                  was={was} onReset={() => put(s.key, s.default)}>
                  {s.kind === 'bool' && <Switch on={s.value === true} label={s.label} onChange={(v) => put(s.key, v)} />}
                  {(s.kind === 'int' || s.kind === 'float') && (
                    <NumberField label={s.label} value={Number(s.value)} min={s.min ?? -1e15} max={s.max ?? undefined} step={s.kind === 'int' ? 1 : 0.1}
                      suffix={s.unit ?? undefined} onCommit={(v) => put(s.key, s.kind === 'int' ? Math.round(v) : v)} />
                  )}
                  {s.kind === 'str' && <TextField label={s.label} value={String(s.value ?? '')} format={(v) => v} parse={(t) => t} onCommit={(v) => put(s.key, v)} />}
                </Row>
              )
            })}
          </div>
        </section>
      ))}
    </>
  )
}
