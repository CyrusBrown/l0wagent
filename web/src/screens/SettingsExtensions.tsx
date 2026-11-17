import { useState } from 'react'
import { problem } from '../api/queries'
import { Row, NumberField, Saved, Switch, TextField } from '../components/settings'
import { Select } from '../components/Select'
import { ExtSlot } from '../ext/ExtElement'
import { useExtensions, useRescan, useSetExtEnabled, useSetExtSettings, type ExtInfo } from '../ext/useExtensions'

/** Settings → Extensions: what is installed, on or off, each one's own settings, and a rescan. */
export function Extensions() {
  const exts = useExtensions()
  const rescan = useRescan()
  return (
    <>
      <section className="set-section">
        <div className="row-between">
          <div className="set-title">Extensions</div>
          <button className="btn" disabled={rescan.isPending} onClick={() => rescan.mutate()}>{rescan.isPending ? 'Scanning…' : 'Rescan'}</button>
        </div>
        <div className="sub" style={{ fontSize: 13 }}>
          Extensions add pages, sidebar sections, cards, previews and commands. They live in the extensions folder on the server; Rescan reads it again after you add or update one.
        </div>
        {rescan.error && <div className="error" role="alert">{problem(rescan.error)}</div>}
        {exts.isError && <div className="error" role="alert">Couldn't load the extensions.</div>}
        {exts.isSuccess && !exts.data.length && <div className="empty">No extensions installed.</div>}
      </section>
      {exts.data?.map((e) => <ExtensionRow key={e.name} e={e} />)}
    </>
  )
}

function ExtensionRow({ e }: { e: ExtInfo }) {
  const toggle = useSetExtEnabled()
  return (
    <section className="set-section ext-row">
      <div className="rows">
        <div className="set-row">
          <div className="stack" style={{ gap: 3, minWidth: 0 }}>
            <span style={{ fontWeight: 500 }}>
              {e.title} <span className="sub">v{e.version}</span>
              {!e.present && <span className="pill warn" style={{ marginLeft: 8 }}>folder missing</span>}
              {!e.compatible && <span className="pill warn" style={{ marginLeft: 8 }}>not compatible</span>}
              {e.enabled && !e.loaded && e.present && e.compatible && <span className="pill mute" style={{ marginLeft: 8 }}>not loaded</span>}
            </span>
            {e.description && <span className="sub">{e.description}</span>}
            <span className="sub">{e.source === 'local' ? 'Local folder' : e.source}</span>
          </div>
          <Switch on={e.enabled} label={`${e.title} enabled`} onChange={(enabled) => { if (!toggle.isPending) toggle.mutate({ name: e.name, enabled }) }} />
        </div>
      </div>
      {toggle.error && <div className="error" role="alert">{problem(toggle.error)}</div>}
      {e.last_error && <pre className="error ext-error">{e.last_error}</pre>}
      {e.enabled && <ExtensionDetails e={e} />}
    </section>
  )
}

function ExtensionDetails({ e }: { e: ExtInfo }) {
  const save = useSetExtSettings()
  const [savedAt, setSavedAt] = useState(0)
  const put = (key: string, value: unknown) => save.mutate({ name: e.name, values: { [key]: value } }, { onSuccess: () => setSavedAt(Date.now()) })
  const panel = e.ui?.settings_panel
  return (
    <>
      {!!e.settings.length && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="set-head"><div className="sub" style={{ fontSize: 13 }}>Settings. Changes save right away.</div><Saved token={savedAt} error={save.error ? problem(save.error) : null} /></div>
          <div className="set-rows one">
            {e.settings.map((s) => {
              const label = s.label || s.key
              const custom = JSON.stringify(s.value) !== JSON.stringify(s.default)
              return (
                <Row key={s.key} label={label} hint={s.help ?? undefined} was={custom ? `Default ${String(s.default)}` : undefined} onReset={() => put(s.key, s.default)}>
                  {s.kind === 'bool' && <Switch on={s.value === true} label={label} onChange={(v) => put(s.key, v)} />}
                  {s.kind === 'choice' && (
                    <Select variant="input" label={label} value={String(s.value)}
                      options={(s.choices ?? []).map((c) => ({ value: String(c), label: String(c) }))}
                      onChange={(v) => put(s.key, (s.choices ?? []).find((c) => String(c) === v) ?? v)} />
                  )}
                  {(s.kind === 'int' || s.kind === 'float') && (
                    <NumberField label={label} value={Number(s.value)} min={s.min ?? -1e15} max={s.max ?? undefined} step={s.kind === 'int' ? 1 : 0.1} onCommit={(v) => put(s.key, s.kind === 'int' ? Math.round(v) : v)} />
                  )}
                  {s.kind === 'str' && <TextField label={label} value={String(s.value ?? '')} format={(v) => v} parse={(t) => t} onCommit={(v) => put(s.key, v)} />}
                </Row>
              )
            })}
          </div>
        </div>
      )}
      {panel && e.loaded && <ExtSlot ext={e} element={panel.element} context={{}} />}
      {!!e.commands.length && (
        <div className="stack" style={{ gap: 6 }}>
          <div className="sub" style={{ fontSize: 13 }}>Commands the assistant can run</div>
          <div className="rows">
            {e.commands.map((c) => (
              <div key={c.name} className="set-row">
                <code>{c.usage || `assistant ${e.name} ${c.name}`}</code>
                <span className="sub">{c.help}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
