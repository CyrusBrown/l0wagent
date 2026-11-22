import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link, useNavigate, useParams } from 'react-router'
import type { S } from '../api/client'
import {
  type BreakerChoice, problem, useApproveFriendRequest, useBlockFriend, useCreateFriendCode, useCreateFriendRequest, useDeclineFriendRequest, useDecideBreaker, useDeleteFriendCode,
  useFriendBreaker, useFriendCodes, useFriendFingerprint, useFriendRequests, useFriendSettings, useFriends, useMembership, usePatchFriend, useUnblockFriend, useUnfriend,
} from '../api/queries'
import { copyText } from '../components/clipboard'
import { ago, clock, pts, stamp } from '../components/format'
import { usePresence } from '../components/motion'
import { useOnline } from '../components/online'
import { Row, Saved, Seg, Switch, TextField } from '../components/settings'
import { useEscape } from '../components/useEscape'

// Friends (hub plan H5): the people whose assistants yours may talk to. Everything that comes from a
// friend (names they chose, notes, presence text, messages) is untrusted text and shown as plain text.

/** `name@hub.domain` → `hub.domain`: the friend's home hub. */
export const homeHub = (address: string) => address.split('@').slice(1).join('@')

/** A friend code `l0wf1:<address>:<16 fingerprint characters>:<secret>` read client-side, only to show what it
 *  holds before it is sent; control checks the code for real. */
export function parseFriendCode(text: string): { address: string; fingerprint: string } | null {
  const m = /^l0wf1:(.+):([0-9A-Za-z]{16}):[A-Za-z0-9_-]+$/.exec(text.trim())
  if (!m) return null
  return { address: m[1], fingerprint: (m[2].toUpperCase().match(/.{4}/g) ?? []).join('-') }
}

const ADDRESS_RE = /^[a-z0-9]+(-[a-z0-9]+)*@[a-z0-9.-]+(:\d+)?$/i

/** A fingerprint in its groups of four, so two people can read it out to each other. */
export function Fingerprint({ value }: { value: string }) {
  const groups = value.includes('-') ? value.split('-') : value.match(/.{1,4}/g) ?? [value]
  return <span className="fr-fp" title="Fingerprint of their signing key">{groups.map((g, i) => <span key={i}>{g}</span>)}</span>
}

/** Presence as a dot and words: "asleep · next: tonight". Nothing when we have none. */
export function PresenceLine({ p }: { p: S['FriendPresence'] | null | undefined }) {
  if (!p) return <span className="fr-presence faint">presence unknown</span>
  const dot = p.state === 'working' ? 'working' : p.state === 'idle' ? 'ok' : ''
  return (
    <span className="fr-presence" title={`As of ${ago(p.at)}`}>
      <span className={`dot ${dot}`} />{p.state}{p.next ? ` · next: ${p.next}` : ''}
    </span>
  )
}

/** The reason a request or action failed, in plain words (control's own words, plus what to do about a key problem). */
export function friendProblem(e: unknown): string | null {
  const text = problem(e)
  if (!text) return null
  const code = (e as { body?: { error?: unknown } } | null)?.body?.error
  if (code === 'fingerprint_mismatch') return `${text} Nothing was sent. Either the code is wrong or old, or someone else is answering to that address. Ask them for a fresh code and compare fingerprints out loud.`
  if (code === 'key_changed' || code === 'hub_key_changed') return `${text} Nothing was sent.`
  return text
}

function NeedHub({ available }: { available: boolean }) {
  return (
    <div className="fr-page">
      <div className="fr-head"><h1>Friends</h1></div>
      <div className="empty-big">
        <div className="empty-title">Friends need a hub</div>
        <div className="sub">
          Friends are other people's assistants, reached through the hub you both belong to. {available ? 'Join a hub and this is where you add them.' : 'This install has no gateway service yet, so it can\'t join a hub: run ./l0w update on the server.'}
        </div>
        <Link className="btn primary" to="/settings/hub" style={{ marginTop: 6 }}>Go to Settings → Hub</Link>
      </div>
    </div>
  )
}

export function Friends() {
  const m = useMembership()
  if (m.isPending) return null
  if (m.data?.state !== 'joined') return <NeedHub available={m.data?.available ?? false} />
  return <FriendsJoined />
}

function FriendsJoined() {
  const address = useParams().address
  // The list and a friend's page slide into each other: forward into the page, back out of it.
  const shown = useRef(address)
  const dir = useRef('')
  if (shown.current !== address) { dir.current = address ? 'view-fwd' : 'view-back'; shown.current = address }
  return (
    <div className="fr-page">
      <div key={address ?? 'list'} className={`fr-view ${dir.current}`}>
        {address ? <FriendDetail address={address} /> : <FriendList />}
      </div>
    </div>
  )
}

// ── The list ──────────────────────────────────────────────────────────────

function FriendList() {
  const friends = useFriends()
  const requests = useFriendRequests()
  const settings = useFriendSettings()
  const fp = useFriendFingerprint()
  const [adding, setAdding] = useState(false)
  const [coding, setCoding] = useState(false)
  const all = friends.data ?? []
  const mine = all.filter((f) => f.state === 'friends')
  const blocked = all.filter((f) => f.state === 'blocked')
  const reqs = (requests.data ?? []).filter((r) => (r.direction === 'in' && r.state === 'pending') || (r.direction === 'out' && ['pending', 'sent', 'failed'].includes(r.state)))
  const cards = reqs.filter((r) => r.state === 'pending')
  const quiet = reqs.filter((r) => r.state !== 'pending')
  const name = (a: string) => all.find((f) => f.address === a)?.display_name ?? a
  return (
    <>
      <div className="fr-head">
        <h1>Friends</h1>
        <div className="btns">
          <button className="btn" onClick={() => setCoding(true)}>Friend code</button>
          <button className="btn primary" onClick={() => setAdding(true)}>Add a friend</button>
        </div>
      </div>
      {settings.data?.paused && (
        <div className="hub-warn" role="status">
          <strong>Friend activity is paused.</strong> Nothing comes in or goes out, and senders try again later. <Link to="/settings/hub">Change it in Settings → Hub</Link>.
        </div>
      )}
      <BreakerCard />
      {(friends.error || requests.error) && <div className="error" role="alert">{problem(friends.error ?? requests.error)}</div>}

      {reqs.length > 0 && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="inbox-group"><span>Requests</span></div>
          {cards.map((r) => <RequestCard key={r.id} r={r} />)}
          {quiet.length > 0 && (
            <div className="fr-list">
              {quiet.map((r) => (
                <div key={r.id} className="fr-row quiet">
                  <div className="stack" style={{ gap: 2, minWidth: 0 }}>
                    <span className="fr-name ellipsis">{name(r.address)}</span>
                    <span className="fr-meta">{r.address}</span>
                  </div>
                  <span className="fr-meta" style={{ textAlign: 'right' }}>
                    {r.state === 'failed' ? <span style={{ color: 'var(--badInk)' }}>Couldn't be delivered{r.error ? `: ${r.error}` : ''}</span> : `You asked ${ago(r.created_at)}. Waiting for them.`}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {friends.isSuccess && mine.length === 0 ? (
        <div className="empty-big">
          <div className="empty-title">No friends yet</div>
          <div className="sub">Add someone by their address, or by a friend code they gave you. Or make a code and hand it to them yourself. Nothing is shared until you say so.</div>
        </div>
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          {mine.length > 0 && <div className="inbox-group"><span>Your friends</span></div>}
          <div className="fr-list">
            {mine.map((f) => <FriendRow key={f.address} f={f} />)}
          </div>
        </div>
      )}

      {blocked.length > 0 && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="inbox-group"><span>Blocked</span></div>
          <div className="fr-list">{blocked.map((f) => <BlockedRow key={f.address} f={f} />)}</div>
        </div>
      )}

      {fp.data && (
        <div className="fr-foot">
          <span className="fr-meta">Your fingerprint · {fp.data.address}</span>
          <Fingerprint value={fp.data.fingerprint} />
          <span className="sub">Friends compare this with what they see on their side. Read it out; don't paste it into a message you can't trust.</span>
        </div>
      )}

      <AddFriend open={adding} close={() => setAdding(false)} />
      <FriendCodePopup open={coding} close={() => setCoding(false)} />
    </>
  )
}

function FriendRow({ f }: { f: S['Friend'] }) {
  const nav = useNavigate()
  return (
    <button className="fr-row" onClick={() => nav(`/friends/${encodeURIComponent(f.address)}`)} aria-label={`${f.display_name}, ${f.address}`}>
      <div className="stack" style={{ gap: 3, minWidth: 0 }}>
        <div className="row-start" style={{ gap: 8, flexWrap: 'wrap' }}>
          <span className="fr-name">{f.display_name}</span>
          {f.muted && <span className="pill mute">Muted</span>}
          {f.key_alarm && <span className="pill warn">Key changed</span>}
        </div>
        <span className="fr-meta">{f.address === f.display_name ? `on ${homeHub(f.address)}` : `${f.address} · ${homeHub(f.address)}`}</span>
        {f.key_alarm && <span className="fr-alarm">{f.key_alarm}</span>}
      </div>
      <PresenceLine p={f.presence} />
    </button>
  )
}

function BlockedRow({ f }: { f: S['Friend'] }) {
  const unblock = useUnblockFriend()
  return (
    <div className="fr-row quiet">
      <div className="stack" style={{ gap: 2, minWidth: 0 }}>
        <span className="fr-name ellipsis">{f.display_name}</span>
        <span className="fr-meta">{f.address}</span>
        {unblock.error && <span className="fr-alarm">{friendProblem(unblock.error)}</span>}
      </div>
      <button className="btn small" disabled={unblock.isPending} onClick={() => unblock.mutate(f.address)}>Unblock</button>
    </div>
  )
}

// ── The spend breaker ─────────────────────────────────────────────────────

/** Shown while the assistant's spending on friends is over a limit and you haven't decided: it slides in, and out once you have.
 *  The numbers are control's estimates; the words are control's own (nothing in it comes from a friend). */
function BreakerCard() {
  const q = useFriendBreaker()
  const decide = useDecideBreaker()
  const online = useOnline()
  const b = q.data
  const open = !!b?.tripped
  const { mounted, closing } = usePresence(open)
  const last = useRef(b)
  if (open) last.current = b  // the card keeps its words while it leaves
  const shown = open ? b : last.current
  if (!mounted && b?.stopped_until && !open) {
    return <div className="hub-warn fr-stopped" role="status">Replies to friends are stopped until {clock(b.stopped_until)}. Their messages still arrive, and presence goes on.</div>
  }
  if (!mounted || !shown) return null
  const label: Record<BreakerChoice, string> = { allow_today: `Allow +${shown.day_step_pct ?? 5}% today`, allow_run: 'Allow for this run', stop_today: 'Stop for today' }
  const busy = decide.isPending
  return (
    <div className={`card card-inbox fr-breaker ${closing ? 'closing' : ''}`} role="region" aria-label="Spending on friends hit a limit">
      <div className="card-label">
        <span>Spending on friends hit a limit</span>
        {shown.at && <span className="r">{ago(shown.at)}</span>}
      </div>
      <div className="card-text">{shown.explanation}</div>
      <div className="fr-meta">
        Today about {pts(shown.day_spent)} of {pts(shown.day_limit)} pts ({shown.day_pct}% of the allowance)
        {shown.run_id != null && shown.run_spent != null && shown.run_limit != null ? ` · run #${shown.run_id}: ${pts(shown.run_spent)} of ${pts(shown.run_limit)} pts` : ''}.
        {' '}Presence is not affected, and these are estimates.
      </div>
      {decide.error && <div className="error" role="alert">{friendProblem(decide.error)}</div>}
      <div className="btns">
        {(shown.choices ?? []).map((c) => (
          <button key={c} className={`btn${c === 'allow_today' ? ' primary' : ''}`} disabled={busy || !online || closing}
            onClick={() => decide.mutate(c)}>{label[c]}</button>
        ))}
      </div>
    </div>
  )
}

// ── Requests ──────────────────────────────────────────────────────────────

function RequestCard({ r }: { r: S['FriendRequest'] }) {
  const approve = useApproveFriendRequest()
  const decline = useDeclineFriendRequest()
  const online = useOnline()
  const incoming = r.direction === 'in'
  const busy = approve.isPending || decline.isPending
  const err = friendProblem(approve.error ?? decline.error)
  return (
    <div className="card card-inbox fr-req">
      <div className="card-label">
        <span>{incoming ? 'Friend request' : 'Your assistant would like to ask'}</span>
        <span className="r">{ago(r.created_at)}</span>
      </div>
      <div className="stack" style={{ gap: 2 }}>
        <span className="fr-name">{r.address}</span>
        <span className="fr-meta">
          {incoming
            ? `on ${homeHub(r.address)} · sent by ${r.made_by === 'assistant' ? 'their assistant' : 'their owner'}${r.code_used ? ' · used your friend code' : ''}`
            : `on ${homeHub(r.address)} · drafted by your assistant; nothing is sent until you approve`}
        </span>
      </div>
      {r.note && <div className="fr-note">{r.note}</div>}
      {r.fingerprint && (
        <div className="stack" style={{ gap: 4 }}>
          <Fingerprint value={r.fingerprint} />
          <span className="sub">{incoming ? 'Their key\'s fingerprint. Compare it with theirs (ask them to read it out) before you approve.' : 'The key their hub lists for that address. Compare it with theirs if you can.'}</span>
        </div>
      )}
      {err && <div className="error" role="alert">{err}</div>}
      <div className="btns">
        <button className="btn primary" disabled={busy || !online} onClick={() => approve.mutate(r.id)}>{approve.isPending ? 'Approving…' : incoming ? 'Approve' : 'Approve and send'}</button>
        <button className="btn" disabled={busy} onClick={() => decline.mutate(r.id)}>Decline</button>
        {incoming && <span className="sub">They aren't told if you decline.</span>}
      </div>
    </div>
  )
}

// ── Popups ────────────────────────────────────────────────────────────────

/** The shell of a centered popup: a portal (the page's entrance animation would otherwise contain a fixed child),
 *  rise-in and sink-out, Esc or a click outside closes it. */
function Popup({ open, close, label, children }: { open: boolean; close: () => void; label: string; children: React.ReactNode }) {
  const { mounted, closing } = usePresence(open)
  useEscape(open, close)
  if (!mounted) return null
  return createPortal(
    <div className={`scrim ${closing ? 'closing' : ''}`} onClick={close}>
      <div className="palette fr-pop" role="dialog" aria-modal="true" aria-label={label} onClick={(e) => e.stopPropagation()}>{children}</div>
    </div>,
    document.body,
  )
}

function AddFriend({ open, close }: { open: boolean; close: () => void }) {
  const create = useCreateFriendRequest()
  const online = useOnline()
  const [text, setText] = useState('')
  const [note, setNote] = useState('')
  const [sent, setSent] = useState<S['FriendRequest'] | null>(null)
  useEffect(() => { if (open) { setText(''); setNote(''); setSent(null); create.reset() } }, [open])  // eslint-disable-line react-hooks/exhaustive-deps
  const value = text.trim()
  const isCode = value.startsWith('l0wf1:')
  const code = isCode ? parseFriendCode(value) : null
  const address = !isCode && ADDRESS_RE.test(value) ? value.toLowerCase() : null
  const ready = !!(code || address) && online && !create.isPending
  const send = () => {
    if (!ready) return
    create.mutate({ ...(code ? { code: value } : { address: address! }), note: note.trim() }, { onSuccess: setSent })
  }
  return (
    <Popup open={open} close={close} label="Add a friend">
      {sent ? (
        <div className="fr-pop-body view-fwd">
          <div className="fr-pop-title">Request sent</div>
          <div className="sub" style={{ fontSize: 13.5 }}>{sent.address} has your request.{sent.fingerprint ? ' The key their hub lists for them:' : ''}</div>
          {sent.fingerprint && <Fingerprint value={sent.fingerprint} />}
          <div className="sub" style={{ fontSize: 12.5 }}>You'll be told when they answer. If you can, compare that fingerprint with theirs.</div>
          <div className="btns"><button className="btn primary" onClick={close}>Done</button></div>
        </div>
      ) : (
        <form className="fr-pop-body" onSubmit={(e) => { e.preventDefault(); send() }}>
          <div className="fr-pop-title">Add a friend</div>
          <label className="stack" style={{ gap: 4 }}>
            <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Friend code or address</span>
            <input className="input mono" autoFocus autoComplete="off" spellCheck={false} placeholder="l0wf1:… or name@hub.example" aria-label="Friend code or address"
              value={text} onChange={(e) => { setText(e.target.value); create.reset() }} />
          </label>
          {code && (
            <div className="fr-parsed hub-sub">
              <div className="row-between"><span className="k">Address</span><span className="mono">{code.address}</span></div>
              <div className="row-between"><span className="k">Fingerprint</span><Fingerprint value={code.fingerprint} /></div>
              <span className="sub">The code carries their key's fingerprint, and sending checks their key against it. If it doesn't match, nothing is sent.</span>
            </div>
          )}
          {isCode && !code && <div className="sub" style={{ color: 'var(--warnInk)' }}>That doesn't look like a whole friend code. Copy it again, all of it.</div>}
          {!isCode && value && !address && <div className="sub">An address looks like name@hub.example.</div>}
          {address && <div className="sub">Without a code their fingerprint isn't checked for you. After they accept, compare it with theirs.</div>}
          <label className="stack" style={{ gap: 4 }}>
            <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>A short note <span className="sub">(they see it)</span></span>
            <textarea className="textarea" rows={2} maxLength={1000} placeholder="Who you are and why" aria-label="Note" value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          {create.error && <div className="error" role="alert">{friendProblem(create.error)}</div>}
          {!online && <div className="sub">You're offline. Requests can only be sent while you're connected.</div>}
          <div className="btns">
            <button className="btn primary" disabled={!ready}>{create.isPending ? 'Sending…' : 'Send'}</button>
            <button type="button" className="btn" onClick={close}>Cancel</button>
          </div>
        </form>
      )}
    </Popup>
  )
}

const EXPIRY: [string, string][] = [['1', '1 day'], ['7', '7 days'], ['14', '14 days'], ['30', '30 days']]

/** Makes a one-time friend code. The code exists only in the answer: it is shown here, once, and closing forgets it. */
function FriendCodePopup({ open, close }: { open: boolean; close: () => void }) {
  const create = useCreateFriendCode()
  const codes = useFriendCodes(open)
  const del = useDeleteFriendCode()
  const [label, setLabel] = useState('')
  const [days, setDays] = useState('14')
  const [copied, setCopied] = useState(false)
  useEffect(() => { if (open) { setLabel(''); setDays('14'); setCopied(false); create.reset() } }, [open])  // eslint-disable-line react-hooks/exhaustive-deps
  const made = create.data
  const active = (codes.data ?? []).filter((c) => c.state === 'active' && c.id !== made?.id)
  return (
    <Popup open={open} close={close} label="Friend code">
      {made ? (
        <div className="fr-pop-body view-fwd">
          <div className="fr-pop-title">Your friend code</div>
          <code className="hub-key" data-testid="friend-code">{made.code}</code>
          <div className="btns">
            <button className="btn" onClick={async () => { if (await copyText(made.code)) setCopied(true) }}>{copied ? 'Copied' : 'Copy'}</button>
            <span className="sub">Works once · expires {stamp(made.expires_at)}</span>
          </div>
          <div className="hub-warn" style={{ fontSize: 13 }}>
            This is the only time you'll see it; it isn't kept. Hand it over out of band: in person, or in a message you trust. Anyone who has it can ask to be your friend.
          </div>
          <div className="stack" style={{ gap: 4 }}>
            <span className="sub">Your address and fingerprint. The code already carries them, and your friend's app checks them. Read the fingerprint out to be sure.</span>
            <div className="row-between"><span className="mono">{made.address}</span><Fingerprint value={made.fingerprint} /></div>
          </div>
          <div className="btns"><button className="btn primary" onClick={close}>Done</button></div>
        </div>
      ) : (
        <form className="fr-pop-body" onSubmit={(e) => { e.preventDefault(); create.mutate({ label: label.trim(), expires_days: Number(days) }) }}>
          <div className="fr-pop-title">Friend code</div>
          <div className="sub" style={{ fontSize: 13.5 }}>A code lets one person ask to be your friend, and lets their app check your key. It works once. Give it to them yourself, not through a place you don't trust.</div>
          <label className="stack" style={{ gap: 4 }}>
            <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Who it's for <span className="sub">(only you see this)</span></span>
            <input className="input" autoComplete="off" maxLength={80} aria-label="Who the code is for" placeholder="Optional" value={label} onChange={(e) => setLabel(e.target.value)} />
          </label>
          <div className="stack" style={{ gap: 4 }}>
            <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Expires after</span>
            <div><Seg value={days} options={EXPIRY} onChange={setDays} label="Expires after" /></div>
          </div>
          {create.error && <div className="error" role="alert">{friendProblem(create.error)}</div>}
          <div className="btns">
            <button className="btn primary" disabled={create.isPending}>{create.isPending ? 'Making…' : 'Make a code'}</button>
            <button type="button" className="btn" onClick={close}>Cancel</button>
          </div>
          {active.length > 0 && (
            <div className="stack" style={{ gap: 6 }}>
              <span className="net-label">Codes that haven't been used</span>
              {active.map((c) => (
                <div key={c.id} className="row-between" style={{ fontSize: 13 }}>
                  <span>{c.label || 'No label'} <span className="sub">· expires {stamp(c.expires_at)}</span></span>
                  <button type="button" className="link-btn" disabled={del.isPending} onClick={() => del.mutate(c.id)}>Cancel code</button>
                </div>
              ))}
            </div>
          )}
        </form>
      )}
    </Popup>
  )
}

// ── One friend ────────────────────────────────────────────────────────────

const SHARE: [S['Friend']['share'], string][] = [['nothing', 'Nothing about my projects'], ['shared', 'Projects we share'], ['anything', 'Anything']]
const DETAIL: [S['Friend']['presence_detail'], string][] = [['coarse', 'Coarse'], ['exact', 'Exact']]
const WAKING: [S['Friend']['waking'], string][] = [['never', 'Never'], ['ask', 'Ask me'], ['allow', 'Allow']]
const BREAKER: [NonNullable<S['Friend']['breaker']>, string][] = [['normal', 'Normal'], ['off', 'No limit']]

export function FriendDetail({ address }: { address: string }) {
  const friends = useFriends()
  const nav = useNavigate()
  const f = friends.data?.find((x) => x.address.toLowerCase() === address.toLowerCase())
  const back = <button className="back-link" onClick={() => nav('/friends')}>← All friends</button>
  if (friends.isPending) return back
  if (!f || (f.state !== 'friends' && f.state !== 'blocked')) {
    return <>{back}<div className="empty-big"><div className="empty-title">Not one of your friends</div><div className="sub">{address} isn't a friend here. They may have been unfriended, or the request is still open.</div></div></>
  }
  return <>{back}{f.state === 'blocked' ? <BlockedDetail f={f} /> : <FriendSettings f={f} />}</>
}

function BlockedDetail({ f }: { f: S['Friend'] }) {
  const unblock = useUnblockFriend()
  return (
    <div className="stack" style={{ gap: 12 }}>
      <h1 className="fr-title">{f.display_name}</h1>
      <div className="sub" style={{ fontSize: 13.5 }}>{f.address} is blocked. Everything they send is dropped without telling them, and they can't wake your assistant.</div>
      {unblock.error && <div className="error" role="alert">{friendProblem(unblock.error)}</div>}
      <div><button className="btn" disabled={unblock.isPending} onClick={() => unblock.mutate(f.address)}>Unblock</button></div>
    </div>
  )
}

function FriendSettings({ f }: { f: S['Friend'] }) {
  const patch = usePatchFriend()
  const block = useBlockFriend()
  const unfriend = useUnfriend()
  const nav = useNavigate()
  const [confirm, setConfirm] = useState<'block' | 'unfriend' | null>(null)
  const [savedAt, setSavedAt] = useState(0)
  // What was just picked shows at once; the saved value replaces it when the answer comes back.
  const pending = patch.isPending ? patch.variables?.patch : undefined
  const save = (p: S['FriendPatch']) => patch.mutate({ address: f.address, patch: p }, { onSuccess: () => setSavedAt((n) => n + 1) })
  const share = pending?.share ?? f.share
  const detail = pending?.presence_detail ?? f.presence_detail
  const waking = pending?.waking ?? f.waking
  const muted = pending?.muted ?? f.muted ?? false
  const breaker = pending?.breaker ?? f.breaker ?? 'normal'
  const p = f.presence
  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="stack" style={{ gap: 8 }}>
        <TextField value={f.display_name} format={(v) => v} parse={(t) => t.trim().slice(0, 80) || null} onCommit={(v) => save({ display_name: v })} label="Display name" width={320} />
        <div className="fr-meta">{f.address} · {homeHub(f.address)} · friends {ago(f.created_at)}{f.same_owner ? ' · says it is your other assistant (their claim)' : ''}</div>
        <PresenceLine p={p} />
        {p?.now && <div className="fr-meta">They say: <span className="fr-untrusted">{p.now}</span></div>}
        {f.fingerprint && (
          <div className="stack" style={{ gap: 4 }}>
            <Fingerprint value={f.fingerprint} />
            <span className="sub">The key you pinned for them. Compare it with theirs now and then.</span>
          </div>
        )}
      </div>

      {f.key_alarm && <div className="hub-warn" role="alert"><strong>Their key changed.</strong> {f.key_alarm}</div>}

      <section className="set-section">
        <div className="set-head"><div className="set-title">What your assistant does for them</div><Saved token={savedAt} error={patch.error ? friendProblem(patch.error) : null} /></div>
        <div className="set-rows one">
          <Row label="What your assistant may share" hint="What it may tell theirs when they ask.">
            <Seg value={share} options={SHARE} onChange={(v) => save({ share: v })} label="What your assistant may share" />
          </Row>
          <Row label="Presence detail" hint="Exact also tells them when you'll next be awake and what you're doing.">
            <Seg value={detail} options={DETAIL} onChange={(v) => save({ presence_detail: v })} label="Presence detail" />
          </Row>
          <Row label="Waking" hint="Whether their assistant may wake yours. Ask me puts each request in your inbox.">
            <Seg value={waking} options={WAKING} onChange={(v) => save({ waking: v })} label="Waking" />
          </Row>
          <Row label="Spend limit" hint="No limit leaves what they cost your assistant out of the spend breaker's limits, and lets it keep writing to them while a limit is hit.">
            <Seg value={breaker} options={BREAKER} onChange={(v) => save({ breaker: v })} label="Spend limit" />
          </Row>
          <Row label="Muted" hint="No notifications from them. Their messages still arrive.">
            <Switch on={muted} onChange={(v) => save({ muted: v })} label="Muted" />
          </Row>
        </div>
      </section>

      <div className="btns">
        {f.thread_id != null
          ? <Link className="btn primary" to={`/t/${f.thread_id}`}>Open conversation</Link>
          : <button className="btn" disabled title="There's no conversation yet">Open conversation</button>}
      </div>

      <section className="set-section hub-danger">
        <div className="set-title">End it</div>
        {confirm === 'unfriend' ? (
          <div className="hub-sub">
            <div className="sub" style={{ fontSize: 13 }}>Unfriend {f.display_name}? They're told, their key is forgotten and neither assistant can reach the other. The conversation stays as history. To be friends again you both start over.</div>
            <div className="btns">
              <button className="btn danger" disabled={unfriend.isPending} onClick={() => unfriend.mutate(f.address, { onSuccess: () => nav('/friends') })}>{unfriend.isPending ? 'Unfriending…' : 'Unfriend'}</button>
              <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
            </div>
            {unfriend.error && <div className="error" role="alert">{friendProblem(unfriend.error)}</div>}
          </div>
        ) : confirm === 'block' ? (
          <div className="hub-sub">
            <div className="sub" style={{ fontSize: 13 }}>Block {f.display_name}? Everything they send is dropped without telling them, and their access is revoked. You can unblock them later from the list.</div>
            <div className="btns">
              <button className="btn danger" disabled={block.isPending} onClick={() => block.mutate(f.address, { onSuccess: () => nav('/friends') })}>{block.isPending ? 'Blocking…' : 'Block'}</button>
              <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
            </div>
            {block.error && <div className="error" role="alert">{friendProblem(block.error)}</div>}
          </div>
        ) : (
          <div className="btns">
            <button className="btn danger" onClick={() => setConfirm('block')}>Block…</button>
            <button className="btn danger" onClick={() => setConfirm('unfriend')}>Unfriend…</button>
          </div>
        )}
      </section>
    </div>
  )
}
