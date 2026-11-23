import { useState } from 'react'
import { problem, useMembership, useMeta } from '../api/queries'
import { Row, NumberField, Saved, Switch, TextField } from '../components/settings'
import { Select } from '../components/Select'
import { ExtSlot } from '../ext/ExtElement'
import { useExtensions, useInstallExt, useRemoveExt, useRescan, useResolveExt, useSetExtEnabled, useSetExtSettings, type ExtInfo } from '../ext/useExtensions'

/** Settings → Extensions: what is installed, on or off, each one's own settings, and a rescan. */
export function Extensions() {
  const exts = useExtensions()
  const rescan = useRescan()
  const ops = !!useMeta().data?.capabilities.ops
  const [installing, setInstalling] = useState(false)
  return (
    <>
      <section className="set-section">
        <div className="row-between">
          <div className="set-title">Extensions</div>
          <div className="btns">
            {ops && !installing && <button className="btn" onClick={() => setInstalling(true)}>Install from git…</button>}
            <button className="btn" disabled={rescan.isPending} onClick={() => rescan.mutate()}>{rescan.isPending ? 'Scanning…' : 'Rescan'}</button>
          </div>
        </div>
        <div className="sub" style={{ fontSize: 13 }}>
          Extensions add pages, sidebar sections, cards, previews and commands. They live in the extensions folder on the server; Rescan reads it again after you add or update one.
        </div>
        {rescan.error && <div className="error" role="alert">{problem(rescan.error)}</div>}
        {ops && installing && <InstallFromGit onDone={() => setInstalling(false)} />}
        {exts.isError && <div className="error" role="alert">Couldn't load the extensions.</div>}
        {exts.isSuccess && !exts.data.length && <div className="empty">No extensions installed.</div>}
      </section>
      {exts.data?.map((e) => <ExtensionRow key={e.name} e={e} />)}
    </>
  )
}

/** "owner/plantlog.git" or "…/plantlog" → "plantlog": a first guess for the name, which must match the extension's own extension.toml. */
export function guessName(git: string): string {
  const last = git.trim().replace(/\/+$/, '').replace(/\.git$/, '').split('/').pop() ?? ''
  return last.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').replace(/^[^a-z]+/, '').slice(0, 31)
}

/** Resolve (the branch or tag becomes a commit, shown in full), then name it and install exactly that commit. It stays off until you turn it on. */
function InstallFromGit({ onDone }: { onDone: () => void }) {
  const resolve = useResolveExt()
  const install = useInstallExt()
  const [git, setGit] = useState('')
  const [ref, setRef] = useState('')
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const commit = resolve.data?.commit
  const forget = () => { resolve.reset(); install.reset() }
  return (
    <form className="set-form ext-install" aria-label="Install from git" onSubmit={(ev) => {
      ev.preventDefault()
      if (!commit) resolve.mutate({ git: git.trim(), ref: ref.trim() }, { onSuccess: () => { if (!name) setName(guessName(git)) } })
      else install.mutate({ name: name.trim(), git: git.trim(), commit, path: path.trim() || null }, { onSuccess: onDone })
    }}>
      <div className="sub" style={{ fontSize: 13 }}>
        An extension runs inside this app, so install only code you trust. You pick a branch or tag, and what gets installed is the one commit it points to right now.
      </div>
      <label className="stack" style={{ gap: 4 }}>
        <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Git address</span>
        <input className="input mono" placeholder="https://github.com/you/plantlog.git" autoComplete="off" spellCheck={false} value={git} onChange={(e) => { setGit(e.target.value); forget() }} />
      </label>
      <label className="stack" style={{ gap: 4 }}>
        <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Branch, tag or commit</span>
        <input className="input mono" placeholder="the default branch" autoComplete="off" spellCheck={false} value={ref} onChange={(e) => { setRef(e.target.value); forget() }} />
      </label>
      {commit && (
        <div className="ext-install-step stack" style={{ gap: 8 }}>
          <div className="stack" style={{ gap: 2 }}>
            <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Commit</span>
            <code className="mono" data-testid="resolved-commit" style={{ fontSize: 12.5, wordBreak: 'break-all' }}>{commit}</code>
          </div>
          <label className="stack" style={{ gap: 4 }}>
            <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Name</span>
            <input className="input" autoComplete="off" spellCheck={false} maxLength={31} value={name} onChange={(e) => setName(e.target.value.toLowerCase())} />
            <span className="sub" style={{ fontSize: 12.5 }}>It must be the name in the extension's own extension.toml (2–31 lowercase letters, digits and dashes).</span>
          </label>
          <label className="stack" style={{ gap: 4 }}>
            <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Folder in the repository (optional)</span>
            <input className="input mono" placeholder="the top, or e.g. extensions/plantlog" autoComplete="off" spellCheck={false} value={path} onChange={(e) => setPath(e.target.value)} />
          </label>
        </div>
      )}
      {(resolve.error || install.error) && <div className="error" role="alert">{problem(resolve.error ?? install.error)}</div>}
      <div className="btns">
        {commit
          ? <button className="btn primary" disabled={!/^[a-z][a-z0-9-]{1,30}$/.test(name.trim()) || install.isPending}>{install.isPending ? 'Installing…' : 'Install'}</button>
          : <button className="btn" disabled={!git.trim() || resolve.isPending}>{resolve.isPending ? 'Looking…' : 'Resolve'}</button>}
        <button type="button" className="btn quiet" onClick={onDone}>Cancel</button>
      </div>
    </form>
  )
}

function ExtensionRow({ e }: { e: ExtInfo }) {
  const toggle = useSetExtEnabled()
  const remove = useRemoveExt()
  const hub = useMembership().data?.hub?.name
  const [confirm, setConfirm] = useState(false)
  const removable = !!e.ops && e.ops.source !== 'hub'
  return (
    <section className="set-section ext-row">
      <div className="rows">
        <div className="set-row">
          <div className="stack" style={{ gap: 3, minWidth: 0 }}>
            <span style={{ fontWeight: 500 }}>
              {e.title} <span className="sub">v{e.version}</span>
              {!e.present && <span className="pill warn" style={{ marginLeft: 8 }}>folder missing</span>}
              {!e.compatible && <span className="pill warn" style={{ marginLeft: 8 }}>not compatible</span>}
              {e.ops?.source === 'hub' && <span className="pill info" style={{ marginLeft: 8 }} title="Installed for a hub service; switch it off under Settings → Hub">from {hub ?? 'your hub'}</span>}
              {e.enabled && !e.loaded && e.present && e.compatible && <span className="pill mute" style={{ marginLeft: 8 }}>not loaded</span>}
            </span>
            {e.description && <span className="sub">{e.description}</span>}
            <span className="sub">{e.source === 'local' ? 'Local folder' : e.source}</span>
          </div>
          <Switch on={e.enabled} label={`${e.title} enabled`} onChange={(enabled) => { if (!toggle.isPending) toggle.mutate({ name: e.name, enabled }) }} />
        </div>
      </div>
      {toggle.error && <div className="error" role="alert">{problem(toggle.error)}</div>}
      {removable && (
        confirm ? (
          <div className="ext-remove" role="group" aria-label={`Remove ${e.title}`}>
            <span className="sub" style={{ fontSize: 13 }}>Remove {e.title}? Its folder is deleted; its data and settings stay in the database.</span>
            <div className="btns">
              <button className="btn danger" disabled={remove.isPending} onClick={() => remove.mutate(e.name, { onSuccess: () => setConfirm(false) })}>{remove.isPending ? 'Removing…' : 'Remove'}</button>
              <button className="btn quiet" onClick={() => setConfirm(false)}>Keep</button>
            </div>
          </div>
        ) : <div><button className="btn small" onClick={() => { remove.reset(); setConfirm(true) }}>Remove…</button></div>
      )}
      {remove.error && <div className="error" role="alert">{problem(remove.error)}</div>}
      {remove.data?.still_in_extensions_toml && <div className="sub" role="status" style={{ fontSize: 13 }}>Removed. Your extensions.toml still lists it, so <code>./l0w extensions sync</code> would fetch it again. Remove its entry there to keep it gone.</div>}
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
