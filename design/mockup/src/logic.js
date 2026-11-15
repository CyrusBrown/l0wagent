class Component extends DCLogic {
  state = { screen: null, ans: {}, cmOpen: {}, cmText: {}, mcSel: null, msSel: [], txt: '', tray: true, waitOpen: true, sideCollapsed: false, filesPanel: false, homeDest: 'chat', homeText: '', trayScope: 'thread', flash: null, nw: null, nwPlace: 'Garden irrigation', nwModel: 'Sonnet', theme: null, palette: false, palQ: '', palKind: 'All', cwd: '/workspace/projects', fileSel: 'garden-irrigation.md', inboxTab: 'waiting', histQ: '', runsTab: 'runs', bannerOff: false, loginStep: 1, locked: false, setupStep: 0, prov: {}, projFilter: 'All', thread: 'Pump controller firmware', projTab: 'overview', runMode: 'simple', panel: false, file: 0, open: { garden: true, tax: false, lab: false }, q1: null, sug: null, inboxFilter: 'All', toolsTab: 'secrets', setTab: 'Budgets & levels', archived: false, thinking: false, level: 'Steady' };

  scrollRef = React.createRef();
  palRef = React.createRef();
  theme() { return this.state.theme || this.props.theme || 'light'; }
  applyTheme() { document.body.dataset.theme = this.theme(); }
  componentDidMount() {
    this.applyTheme();
    this._key = e => {
      const st = this.state, typing = /INPUT|TEXTAREA/.test((e.target && e.target.tagName) || '');
      if (e.key === 'Escape') { if (st.palette) this.setState({ palette: false }); else if (st.nw) this.setState({ nw: null }); return; }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); this.setState({ palette: !st.palette, palQ: '', nw: null }); return; }
      if (typing || e.metaKey || e.ctrlKey || st.palette || st.nw) return;
      const k = e.key.toLowerCase();
      if (k === 'n') { e.preventDefault(); this.go('new', this.state.homeText.trim() ? {} : { homeDest: 'chat' }); }
      if (k === 'c') { e.preventDefault(); this.go('new', { homeDest: 'chat' }); }
      if (k === 'j') this.go('thread', { thread: 'Moisture sensor calibration' });
    };
    window.addEventListener('keydown', this._key);
  }
  componentDidUpdate(pp, ps) { this.applyTheme(); if (this.state.palette && !ps.palette) setTimeout(() => this.palRef.current && this.palRef.current.focus(), 0); }
  componentWillUnmount() { window.removeEventListener('keydown', this._key); clearTimeout(this._ft); }
  jump(id) {
    const sc = this.scrollRef.current, el = sc && sc.querySelector('#card-' + id);
    if (el) sc.scrollTo({ top: el.offsetTop - 16, behavior: 'smooth' });
    this.setState({ flash: id }); clearTimeout(this._ft);
    this._ft = setTimeout(() => this.setState({ flash: null }), 1500);
  }
  go(screen, extra) { this.setState({ screen, panel: false, ...(extra || {}) }); }

  renderVals() {
    const s = this.state;
    const screen = s.screen || this.props.startScreen || 'home';
    const mode = this.props.assistantState || 'asleep';
    const act = 'var(--active)';
    const sel = (on) => on ? act : 'transparent';
    const goThread = () => this.go('thread', { thread: 'Pump controller firmware' });

    const projDefs = [
      { key: 'garden', name: 'Garden irrigation', threads: ['Pump controller firmware', 'Moisture sensor calibration', 'Winter shutdown checklist', 'Flow meter options'], archived: ['Enclosure v2 print'] },
      { key: 'tax', name: 'Tax prep 2026', threads: ['Receipts to categorize', 'Questions for the accountant'], archived: [] },
      { key: 'lab', name: 'Home lab', threads: ['Email setup', 'NAS backup schedule'], archived: [] },
    ];
    const unreadMap = { 'Moisture sensor calibration': 2, 'Flow meter options': 1, 'Receipts to categorize': 1 };
    const projects = projDefs.map(p => ({
      name: p.name, count: p.threads.length, caret: s.open[p.key] ? '▾' : '▸', expanded: !!s.open[p.key], rows: s.open[p.key] ? '1fr' : '0fr', op: s.open[p.key] ? 1 : 0,
      bg: 'transparent',
      newThread: (e) => { e.stopPropagation(); this.go('thread', { thread: 'New thread', ntProj: p.name, open: { ...s.open, [p.key]: true } }); },
      toggle: (e) => { e.stopPropagation(); this.setState(st => ({ open: { ...st.open, [p.key]: !st.open[p.key] } })); },
      open: () => this.go('project', { open: { ...s.open, [p.key]: true } }),
      threads: (s.ntProj === p.name && s.thread === 'New thread' ? ['New thread', ...p.threads] : p.threads).map(t => { const u = unreadMap[t] || 0; return { name: t, unread: u || false, fw: u ? 600 : 400, color: u ? 'var(--fg)' : 'var(--fg2)', bg: sel(screen === 'thread' && s.thread === t), open: () => this.go('thread', { thread: t }) }; }),
      unread: p.threads.reduce((a, t) => a + (unreadMap[t] || 0), 0) || false,
    }));
    const chats = [
      { name: 'Setting up the Fastmail MCP', meta: 'Home lab · 2 h ago' },
      { name: 'Convert feed rates to metric', meta: '1 d ago' },
      { name: 'Scaling the sourdough recipe', meta: 'Nov 9' },
    ].map((c, i) => ({ ...c, bg: sel(screen === 'chat' && i === 0), open: () => this.go('chat') }));
    const footer = [['Inbox', 'inbox', 7], ['Runs', 'runs'], ['Calendar', 'calendar'], ['Tools & Secrets', 'tools'], ['Settings', 'settings']]
      .map(([label, k, count]) => ({ label, count: count || false, bg: sel(screen === k || (k === 'runs' && screen === 'run')), open: () => this.go(k) }));

    const chips = {
      working: { dot: '#c4673f', anim: 'pulse 1.6s ease-in-out infinite', state: 'Working', text: 'Running the DST test suite on the patched scheduler', budget: '3.8 / 10 pts', running: true },
      asleep: { dot: 'var(--faint2)', anim: 'none', state: 'Asleep', text: 'next wakeup Sat 02:00', budget: '42 pts left this week', running: false },
      dormant: { dot: 'var(--dim)', anim: 'none', state: 'Dormant', text: 'no wakeups planned', budget: '42 pts left this week', running: false },
      offline: { dot: '#b8862f', anim: 'none', state: 'Offline', text: 'last synced 4 min ago', budget: '3 messages in outbox', running: false },
    };
    const deliveries = {
      working: { dot: '#5a8a5e', text: 'Working now, sees it within a minute', canReply: false },
      asleep: { dot: 'var(--faint2)', text: 'Queued for next wakeup (Sat 02:00)', canReply: true },
      dormant: { dot: 'var(--dim)', text: 'Dormant. It will see this when you wake it.', canReply: true },
      offline: { dot: '#b8862f', text: 'Waiting for connection (on this device)', canReply: false },
    };

    const crumbs = { new: 'New', home: 'Home', projects: 'Projects', files: 'Files', network: 'Network', web: 'Web page', login: 'Login', setup: 'Setup', thread: 'Garden irrigation  /  ' + s.thread, project: 'Garden irrigation', inbox: 'Inbox', calendar: 'Calendar', runs: 'Runs', run: 'Runs  /  #123', chat: 'Chats  /  Setting up the Fastmail MCP', tools: 'Tools & Secrets', settings: 'Settings' };
    const is = {}; Object.keys(crumbs).forEach(k => is[k] = screen === k);

    const tabRow = (list, cur, key) => list.map(([id, label]) => ({ label, color: cur === id ? 'var(--fg)' : 'var(--faint)', line: cur === id ? 'var(--fg)' : 'transparent', open: () => this.setState({ [key]: id }) }));
    const flags = (list, cur) => { const o = {}; list.forEach(([id]) => o[id] = id === cur); return o; };
    const pTabs = [['overview', 'Overview'], ['threads', 'Threads'], ['files', 'Files'], ['asks', 'Asks · 2'], ['activity', 'Activity'], ['ext', 'Plant log']];
    const tTabs = [['secrets', 'Secrets'], ['integrations', 'Integrations'], ['network', 'Network rules'], ['apps', 'Connected apps']];

    const fileDefs = [
      { name: 'scheduler.py', ext: 'PY', source: '~/garden/firmware/scheduler.py', mode: 'Live', by: 'Assistant', when: '12 min ago', versions: 'v6 of 6', isCode: true },
      { name: 'enclosure-v3.stl', ext: 'STL', source: 'inbox/enclosure-v3.stl', mode: 'Snapshot', by: 'From you', when: 'Nov 10', isPlaceholder: true, placeholder: '3D viewer · enclosure-v3.stl', hint: 'drag to orbit · scroll to zoom' },
      { name: 'pump-log-1102.csv', ext: 'CSV', source: 'inbox/pump-log-1102.csv', mode: 'Snapshot', by: 'From you', when: 'Nov 12', isPlaceholder: true, placeholder: 'CSV table preview', hint: '1,204 rows' },
      { name: 'Project doc', ext: 'MD', source: 'notes/garden-irrigation.md', mode: 'Live', by: 'Assistant', when: '1 h ago', versions: 'v14 of 20', isMd: true },
      { name: 'Moisture dashboard', ext: 'HTML', source: '~/garden/out/dashboard.html', mode: 'Live', by: 'Assistant', when: '2 d ago', isPlaceholder: true, placeholder: 'sandboxed frame', hint: 'agent-made HTML renders on a separate origin' },
      { name: 'wiring-diagram.png', ext: 'PNG', source: 'inbox/wiring-diagram.png', mode: 'Snapshot', by: 'From you', when: 'Oct 28', isPlaceholder: true, placeholder: 'image preview', hint: '2400 × 1600' },
    ];
    const files = fileDefs.map((f, i) => ({ ...f, bg: i === s.file ? 'var(--rowh)' : 'var(--card)', modeColor: f.mode === 'Live' ? 'var(--okInk)' : 'var(--muted)', select: () => this.setState({ file: i }) }));
    const fsel = fileDefs[s.file];
    const file = { isCode: false, isPlaceholder: false, isMd: false, versions: false, ...fsel };

    const iFilters = ['All', 'Questions', 'Permissions', 'Suggestions', 'Promote', 'Security'];
    const inboxAll = [
      { title: 'Questions', cat: ['Questions'], items: [
        { where: '#Pump controller firmware', when: '12 min ago', text: 'Should I flash the fix to the controller myself once tests pass?', primary: 'Flash it', secondary: 'Leave it for me' },
        { where: '#Pump controller firmware', when: '12 min ago', type: 'single', text: 'Which schedule should the firmware use on Sundays?', opts: ['Keep 06:00 every day', 'Move to 05:30, outside the DST hour', 'Skip Sundays entirely'], primary: 'Submit', secondary: 'Skip' },
        { where: '#Receipts to categorize', when: 'Wed', type: 'multi', text: 'Which of these should be filed as deductible?', opts: ['Plumber, March', 'Laptop, May', 'Glasses, August', 'Desk chair, June', 'Train pass, all year'], primary: 'Submit', secondary: 'Skip' },
        { where: '#Pump controller firmware', when: '11 min ago', type: 'text', text: 'Is there a minimum rest time the pump needs between cycles?', primary: 'Send answer', secondary: 'Skip' },
        { where: '#NAS backup schedule', when: 'Thu', type: 'slot', text: 'When should I run the first full NAS backup? It takes about 3 hours and slows the network.', opts: ['Tonight 23:00', 'Sat 02:00', 'Sun 03:00', 'Mon 01:00'], primary: 'Schedule', secondary: 'Not this week' },
        { where: 'Settings · Reviewer', when: 'Tue', type: 'scale', text: 'How strict should the reviewer be with my plans?', opts: ['1', '2', '3', '4', '5'], lo: 'Light touch', hi: 'Question everything', primary: 'Submit', secondary: 'Skip' } ] },
      { title: 'Permissions & tasks', cat: ['Permissions'], items: [
        { where: '#Pump controller firmware', when: '14 min ago', text: 'Write access to ~/garden/firmware/ for this project', primary: 'Allow for project', secondary: 'Deny' },
        { where: '#Receipts to categorize', when: 'Thu', text: 'Mark “Review the 3 unreadable receipts” as done?', primary: 'Done', secondary: 'Decline' } ] },
      { title: 'Thread suggestions', cat: ['Suggestions'], acceptAll: true, items: [
        { where: '#Pump controller firmware', when: '20 min ago', text: 'Split the sensor calibration messages into “Moisture sensor calibration”', primary: 'Accept', secondary: 'Dismiss' },
        { where: '#Flow meter options', when: 'Wed', text: 'Archive “Enclosure v2 print”, untouched for 3 weeks', primary: 'Accept', secondary: 'Dismiss' } ] },
      { title: 'Promote requests', cat: ['Promote'], items: [
        { where: '#Pump controller firmware', when: '8 min ago', text: 'Turn this reply into a full run: audit every timer for DST issues, about 3 pts', primary: 'Approve', secondary: 'Deny' } ] },
      { title: 'Security', cat: ['Security'], items: [
        { where: 'Account', when: 'Today 09:14', text: 'New session: Firefox on Linux, 192.168.1.40', primary: 'That was me', secondary: 'Revoke' } ] },
    ];
    const ibSel = s.ibSel || {}, ibText = s.ibText || {};
    const ibAnswer = (id, v) => this.setState(st => ({ ans: { ...st.ans, [id]: v } }));
    const inboxGroups = inboxAll.map((g, gi) => ({ ...g, items: g.items.map((it, ii) => { const id = 'ib' + gi + '-' + ii, t = s.cmText[id] || ''; const ty = it.type || 'buttons', sv = ibSel[id], setSel = v => this.setState(st => ({ ibSel: { ...(st.ibSel || {}), [id]: v } }));
      const opts = (it.opts || []).map(o => { const on = ty === 'multi' ? (sv || []).includes(o) : sv === o;
        return { label: o, border: on ? '#c4673f' : 'var(--line)', bg: on ? 'var(--accentSoft)' : 'var(--card)', ring: on ? '#c4673f' : 'var(--dim)', dot: on ? '#c4673f' : 'transparent', box: on ? '#c4673f' : 'transparent', check: on ? '✓' : '',
          pick: () => setSel(ty === 'multi' ? ((sv || []).includes(o) ? sv.filter(x => x !== o) : [...(sv || []), o]) : o) }; });
      const submit = () => {
        if (ty === 'buttons') return ibAnswer(id, it.primary);
        if (ty === 'text') { const t = (ibText[id] || '').trim(); return t && ibAnswer(id, 'You replied: “' + t + '”'); }
        if (ty === 'multi') return sv && sv.length && ibAnswer(id, 'You picked: ' + sv.join(', '));
        if (ty === 'scale') return sv && ibAnswer(id, 'You picked ' + sv + ' of 5');
        return sv && ibAnswer(id, (ty === 'slot' ? 'Scheduled: ' : 'You picked: ') + sv);
      };
      return { ...it, opts, lo: it.lo || '', hi: it.hi || '', isSingle: ty === 'single', isMulti: ty === 'multi', isText: ty === 'text', isScale: ty === 'scale', isSlot: ty === 'slot',
      textVal: ibText[id] || '', textChange: e => { const v = e.target.value; this.setState(st => ({ ibText: { ...(st.ibText || {}), [id]: v } })); },
      open: !s.ans[id], done: !!s.ans[id], answer: s.ans[id],
      a: submit, b: () => ibAnswer(id, it.secondary),
      cmOpen: !!s.cmOpen[id], cmRows: s.cmOpen[id] ? '1fr' : '0fr', cmOp: s.cmOpen[id] ? 1 : 0, cmMt: s.cmOpen[id] ? '0px' : '-10px', cmText: t, hasCm: !!t.trim(), cmLabel: s.cmOpen[id] ? 'Remove comment' : '+ Add comment',
      cmToggle: () => this.setState(st => ({ cmOpen: { ...st.cmOpen, [id]: !st.cmOpen[id] }, cmText: st.cmOpen[id] ? { ...st.cmText, [id]: '' } : st.cmText })),
      cmChange: e => { const v = e.target.value; this.setState(st => ({ cmText: { ...st.cmText, [id]: v } })); } }; }) }))
      .filter(g => s.inboxFilter === 'All' || g.cat.includes(s.inboxFilter)).map(g => ({ acceptAll: false, ...g }));
    const pill = (on) => ({ border: on ? 'var(--fg)' : 'var(--line2)', bg: on ? 'var(--fg)' : 'var(--card)', color: on ? 'var(--bg)' : 'var(--fg2)' });

    const H = 28, ev = (start, end, kind, label, meta) => ({ start, end, kind, label, meta: meta || '' });
    const kinds = {
      wakeup: { bg: 'var(--accentSoft)', border: 'var(--accentLine)', color: 'var(--accentInk)', bs: 'solid' },
      routine: { bg: 'var(--infoSoft)', border: 'var(--infoLine)', color: 'var(--infoInk)', bs: 'solid' },
      sweep: { bg: 'var(--soft)', border: 'var(--line3)', color: 'var(--muted)', bs: 'solid' },
      fallback: { bg: 'var(--bg)', border: 'var(--dim)', color: 'var(--muted)', bs: 'dashed' },
      protected: { bg: 'repeating-linear-gradient(135deg,var(--soft) 0 5px,var(--bg) 5px 10px)', border: 'transparent', color: 'var(--faint)', bs: 'solid' },
      mine: { bg: 'var(--okSoft)', border: 'var(--okLine)', color: 'var(--okInk)', bs: 'solid' },
    };
    const dayDefs = [
      ['Mon', 9, [ev(2, 3, 'wakeup', 'Garden: trace logs', '6 pts'), ev(7, 7.5, 'routine', 'Inbox triage')]],
      ['Tue', 10, [ev(7, 7.5, 'routine', 'Inbox triage'), ev(13, 13.5, 'sweep', 'Sweep')]],
      ['Wed', 11, [ev(3, 4.5, 'wakeup', 'Tax: categorize', '8 pts'), ev(7, 7.5, 'routine', 'Inbox triage')]],
      ['Thu', 12, [ev(7, 7.5, 'routine', 'Inbox triage'), ev(10, 11.5, 'mine', 'You: firmware chat'), ev(13, 13.5, 'sweep', 'Sweep')]],
      ['Fri', 13, [ev(7, 7.5, 'routine', 'Inbox triage'), ev(22.5, 23.5, 'fallback', 'Fallback', '2 pts')]],
      ['Sat', 14, [ev(2, 3.5, 'wakeup', 'Pump fix · run #123', '10 pts'), ev(13, 13.5, 'sweep', 'Sweep')]],
      ['Sun', 15, [ev(6, 7, 'routine', 'Weekly digest', '3 pts')]],
    ];
    const days = dayDefs.map(([name, date, evs]) => ({
      name, date, color: date === 14 ? 'var(--accentInk)' : 'var(--fg2)', bg: date === 14 ? 'var(--today)' : 'var(--card)',
      events: [ev(18, 22, 'protected', 'Protected'), ...evs].map(e => { const k = kinds[e.kind]; return { label: e.label, title: e.meta ? e.label + ' · ' + e.meta : e.label, top: e.start * H + 'px', h: Math.max(16, (e.end - e.start) * H - 2) + 'px', bg: k.bg, border: k.border, color: k.color, borderStyle: k.bs }; }),
    }));
    const legend = [['Planned wakeup', 'wakeup'], ['Routine', 'routine'], ['Sweep', 'sweep'], ['Fallback', 'fallback'], ['Yours', 'mine'], ['Protected hours', 'protected']].map(([label, k]) => ({ label, bg: kinds[k].bg, border: kinds[k].border === 'transparent' ? 'var(--line3)' : kinds[k].border }));

    const statusColors = { Done: ['var(--okInk)', 'var(--okSoft)'], Running: ['var(--accentInk)', 'var(--accentSoft)'], Partial: ['var(--warnInk)', 'var(--warnSoft)'], Stopped: ['var(--muted)', 'var(--soft)'], Dropped: ['var(--muted)', 'var(--soft)'], 'In progress': ['var(--accentInk)', 'var(--accentSoft)'], Planned: ['var(--infoInk)', 'var(--infoSoft)'] };
    const runs = [
      [123, 'Fix the Sunday double pump trigger, then file receipts', 'Planned wakeup', 'scheduled Sat 02:00', 'Today 02:00', '18 min so far', 3.8, 10, 'Running'],
      [122, 'Weekly digest across all projects', 'Routine', 'weekly digest', 'Sun 06:00', '9 min', 2.6, 3, 'Done'],
      [121, 'Categorize 41 receipts for Tax prep 2026', 'Planned wakeup', 'new files in inbox', 'Wed 03:00', '1 h 12 min', 7.9, 8, 'Partial'],
      [120, 'Sweep: stale threads and suggestions', 'Sweep', 'sweep slot', 'Tue 13:00', '6 min', 0.8, 1.5, 'Done'],
      [119, 'Trace pump logs, find the double trigger', 'Promoted reply', 'you approved', 'Mon 02:00', '41 min', 5.5, 6, 'Done'],
      [118, 'Fallback check-in', 'Fallback', 'no wakeup for 48 h', 'Sat 23:00', '3 min', 0.4, 2, 'Stopped'],
    ].map(([id, about, kind, why, start, dur, used, given, status]) => ({ id, about, kind, why, start, dur, used, given, status, pct: Math.round(used / given * 100) + '%', sColor: statusColors[status][0], sBg: statusColors[status][1], openRun: () => this.go('run') }));

    const claims = [
      ['#Pump controller firmware', 'Switch fallback timer to UTC and add a 10-minute debounce', 'Patched scheduler.py · 4 of 7 DST tests passing', 'In progress'],
      ['#Receipts to categorize', 'File the 6 receipts that arrived this week', 'Waiting for budget after the firmware fix', 'Planned'],
    ].map(([thread, title, result, status]) => ({ thread, title, result, status, sColor: statusColors[status][0], sBg: statusColors[status][1] }));
    const tl = (t, text, kind) => ({ t, text, tag: kind === 'control' ? 'control' : kind === 'ask' ? 'ask' : 'note', dot: kind === 'control' ? '#6b8fb8' : kind === 'ask' ? '#b8862f' : 'var(--dim)', color: kind === 'control' ? 'var(--infoInk)' : 'var(--fg)' });
    const statusTimeline = [
      tl('02:00', 'Woke for planned slot. Level Steady, reviewer every 10 min.', 'control'),
      tl('02:03', 'Reproduced the double trigger with a fake DST clock.'),
      tl('02:07', 'Filed a question: flash the fix or leave it?', 'ask'),
      tl('02:09', 'Reviewer interjected: also check the rain-delay timer.', 'control'),
      tl('02:12', 'Patched scheduler.py to compare UTC times.'),
      tl('02:14', 'Shelved scheduler.py (live, keeping versions).', 'control'),
      tl('02:16', 'Model changed: effort medium → high for the test pass.', 'control'),
    ];
    const changes = [
      { group: 'Memory', items: [{ name: 'projects/garden.md', add: '+12', del: '−3' }, { name: 'people/mara.md', add: '+1', del: '' }] },
      { group: 'Work files', items: [{ name: 'firmware/scheduler.py', add: '+18', del: '−6' }, { name: 'firmware/test_dst.py', add: '+64', del: '' }] },
      { group: 'Shelved', items: [{ name: 'scheduler.py · live', add: '', del: '' }] },
      { group: 'Asks filed', items: [{ name: 'Flash the fix?', add: '', del: '' }, { name: 'Write access', add: '', del: '' }] },
      { group: 'Threads', items: [{ name: 'Suggested: split calibration', add: '', del: '' }] },
    ];
    const dItems = [
      { t: '02:00:00', kind: 'Control', text: 'Run started · planned wakeup · budget 10 pts · deadline 02:45' },
      { t: '02:00:04', kind: 'Thinking', text: 'The Sunday log shows the fallback firing 4 s after the main trigger. Probably a local-time comparison.', thinking: true },
      { t: '02:00:09', kind: 'Tools ×3', text: 'read_file scheduler.py · read_file pump-log-1102.csv · grep "fallback"' },
      { t: '02:03:40', kind: 'Message', text: 'Reproduced with a fake clock set to Nov 1 01:59 → 03:00.' },
      { t: '02:07:12', kind: 'Ask', text: 'Filed question in #Pump controller firmware: flash the fix or leave it?' },
      { t: '02:09:30', kind: 'Reviewer', text: 'Reviewer: the rain-delay timer uses the same comparison. Include it or note it as a follow-up.' },
      { t: '02:09:58', kind: 'Reviewer', text: 'Worker: noted, will include it in the same patch.' },
      { t: '02:12:05', kind: 'Diff', text: 'firmware/scheduler.py  +18 −6', code: '-    if now.hour == slot.hour and now.minute == slot.minute:\n+    now_utc = now.astimezone(timezone.utc)\n+    if abs((now_utc - slot.utc).total_seconds()) < DEBOUNCE_S:' },
      { t: '02:16:00', kind: 'Model', text: 'Effort medium → high (reviewer approved tier change)' },
      { t: '02:17:30', kind: 'Usage', text: 'Usage sample: 3.8 pts used, 38% of budget at 40% of time' },
    ];
    const kc = { Control: 'var(--infoInk)', Thinking: 'var(--faint)', Reviewer: 'var(--purpleInk)', Ask: 'var(--warnInk)', Diff: 'var(--okInk)', Model: 'var(--infoInk)', Usage: 'var(--faint)' };
    const detailItems = dItems.filter(d => s.thinking || !d.thinking).map(d => ({ code: false, ...d, kColor: kc[d.kind] || 'var(--muted)', fs: d.thinking ? 'italic' : 'normal', rowBg: d.kind === 'Reviewer' ? 'var(--purpleSoft)' : 'var(--card)' }));

    const levelDefs = { Frugal: ['40 pts/wk', 'Wakes only for planned work and replies. Long tasks get split over several nights.'], Steady: ['120 pts/wk', 'Wakes a few times a day when there is work, uses a mid-size model for most of it, and keeps a reserve for your replies.'], Busy: ['250 pts/wk', 'Wakes whenever new work arrives and runs longer reviews. Good for a week with a deadline.'], Max: ['500 pts/wk', 'Uses the full provider allowance. Reserves still protect your own usage.'] };
    const levels = Object.keys(levelDefs).map(n => ({ name: n, pts: levelDefs[n][0], border: s.level === n ? '#c4673f' : 'var(--line)', pick: () => this.setState({ level: n }) }));
    const setTabList = ['Appearance', 'Budgets & levels', 'Providers', 'Roles', 'Reviewer', 'Standing instructions', 'Account', 'Notifications', 'Extensions', 'System'];

    const pol = (p) => ({ a: p === 'a' ? 'var(--card)' : 'transparent', k: p === 'k' ? 'var(--card)' : 'transparent', b: p === 'b' ? 'var(--card)' : 'transparent' });


    const answer = (id, v) => this.setState(st => ({ ans: { ...st.ans, [id]: v } }));
    const pend = [['q1', 'Question', 'Flash the fix to the controller once tests pass?'], ['mc', 'Pick one', 'Which schedule should the firmware use on Sundays?'], ['ms', 'Pick any', 'Which zones get the 10-minute debounce?'], ['txt', 'Written', 'Minimum rest time between pump cycles?'], ['perm', 'Permission', 'Write access to ~/garden/firmware/'], ['sug', 'Suggestion', 'Split sensor calibration into its own thread'], ['promo', 'Promote', 'Turn this reply into a full run, about 3 pts'], ['sec', 'Secure input', 'MQTT broker password']];
    const c = {};
    pend.forEach(([id]) => {
      const t = s.cmText[id] || '';
      c[id] = { open: !s.ans[id], done: !!s.ans[id], answer: s.ans[id], cmOpen: !!s.cmOpen[id], cmRows: s.cmOpen[id] ? '1fr' : '0fr', cmOp: s.cmOpen[id] ? 1 : 0, cmMt: s.cmOpen[id] ? '0px' : '-10px', cmText: t, hasCm: !!t.trim(),
        cmLabel: s.cmOpen[id] ? 'Remove comment' : '+ Add comment',
        cmToggle: () => this.setState(st => ({ cmOpen: { ...st.cmOpen, [id]: !st.cmOpen[id] }, cmText: st.cmOpen[id] ? { ...st.cmText, [id]: '' } : st.cmText })),
        cmChange: e => { const v = e.target.value; this.setState(st => ({ cmText: { ...st.cmText, [id]: v } })); },
        border: s.flash === id ? '#c4673f' : 'var(--line)' };
    });
    c.q1.a = () => answer('q1', 'You answered: Flash it'); c.q1.b = () => answer('q1', 'You answered: Leave it for me');
    c.mc.opts = ['Keep 06:00 every day', 'Move to 05:30, outside the DST hour', 'Skip Sundays entirely', 'Something else (explain in a comment)'].map(o => { const on = s.mcSel === o; return { label: o, pick: () => this.setState({ mcSel: o }), border: on ? '#c4673f' : 'var(--line)', bg: on ? 'var(--accentSoft)' : 'var(--card)', ring: on ? '#c4673f' : 'var(--dim)', dot: on ? '#c4673f' : 'transparent' }; });
    c.mc.submit = () => s.mcSel && answer('mc', 'You picked: ' + s.mcSel);
    c.ms.opts = ['Back lawn', 'Front beds', 'Greenhouse', 'Herb planter'].map(o => { const on = s.msSel.includes(o); return { label: o, pick: () => this.setState(st => ({ msSel: st.msSel.includes(o) ? st.msSel.filter(x => x !== o) : [...st.msSel, o] })), border: on ? '#c4673f' : 'var(--line)', bg: on ? 'var(--accentSoft)' : 'var(--card)', box: on ? '#c4673f' : 'transparent', boxLine: on ? '#c4673f' : 'var(--dim)', check: on ? '✓' : '' }; });
    c.ms.submit = () => s.msSel.length && answer('ms', 'You picked: ' + s.msSel.join(', '));
    c.ms.count = s.msSel.length ? s.msSel.length + ' selected' : 'select all that apply';
    c.txt.value = s.txt; c.txt.change = e => this.setState({ txt: e.target.value });
    c.txt.submit = () => s.txt.trim() && answer('txt', 'You replied: “' + s.txt.trim() + '”');
    c.perm.a = () => answer('perm', 'Allowed for this project'); c.perm.b = () => answer('perm', 'Allowed once'); c.perm.c = () => answer('perm', 'Denied');
    c.sug.a = () => answer('sug', 'Accepted · new thread created'); c.sug.b = () => answer('sug', 'Dismissed');
    c.promo.a = () => answer('promo', 'Approved · run #124 starting'); c.promo.b = () => answer('promo', 'Denied');
    c.sec.a = () => answer('sec', 'Saved as MQTT_PASSWORD');
    const threadPend = pend.filter(([id]) => !s.ans[id]).map(([id, kind, text]) => ({ kind, text, where: '', cta: 'Jump to', jump: () => this.jump(id) }));
    const otherPend = [['Task', 'Mark “Review the 3 unreadable receipts” done?', '#Receipts'], ['Suggestion', 'Archive “Enclosure v2 print”', '#Flow meter'], ['Security', 'New session: Firefox on Linux', 'Account']].map(([kind, text, where]) => ({ kind, text, where, cta: 'Open', jump: () => this.go('inbox') }));
    const tray = { open: s.tray, dot: threadPend.length ? '#c4673f' : 'var(--faint2)', title: threadPend.length ? threadPend.length + ' waiting on you' : 'Nothing waiting on you here', summary: [...new Set(threadPend.map(p => p.kind))].join(' · '), caret: s.tray ? 'Hide' : 'Show' };
    const trayScopes = [['thread', 'This thread · ' + threadPend.length], ['all', 'Everywhere · ' + (threadPend.length + otherPend.length)]].map(([k, l]) => ({ label: l, ...pill(s.trayScope === k), pick: () => this.setState({ trayScope: k }) }));
    const topNav = [['Conversations', 'thread'], ['Inbox', 'inbox', 7], ['Runs', 'runs'], ['Files', 'files'], ['Tools', 'tools']].map(([label, k, count]) => { const on = screen === k || (k === 'runs' && screen === 'run') || (k === 'thread' && ['thread', 'project', 'projects', 'chat', 'new'].includes(screen)) || false; return { label, count: count || false, color: on ? 'var(--fg)' : 'var(--muted)', bg: on ? 'var(--active)' : 'transparent', open: () => this.go(k) }; });
    const theme = this.theme();
    const isT = s.nw !== 'chat';
    const nw = { op: s.nw ? 1 : 0, vis: s.nw ? 'visible' : 'hidden', tf: s.nw ? 'none' : 'translateY(10px) scale(.97)', open: !!s.nw, isThread: isT, close: () => this.setState({ nw: null }),
      threadBg: isT ? 'var(--card)' : 'transparent', chatBg: isT ? 'transparent' : 'var(--card)',
      pickThread: () => this.setState({ nw: 'thread' }), pickChat: () => this.setState({ nw: 'chat' }),
      pickLabel: isT ? 'Where' : 'Model',
      kindHint: isT ? 'The assistant works on threads during runs' : 'A 1:1 chat with live replies',
      chips: isT ? ['General', 'Garden irrigation', 'Tax prep 2026', 'Home lab'].map(p => ({ label: p, ...pill(s.nwPlace === p), pick: () => this.setState({ nwPlace: p }) }))
                 : ['Haiku · fast', 'Sonnet', 'Opus · slower'].map(m => ({ label: m, ...pill(s.nwModel === m), pick: () => this.setState({ nwModel: m }) })),
      placeholder: isT ? 'What should the assistant work on? Use @project, #thread or /file to link.' : 'Ask anything',
      note: isT ? deliveries[mode].text : 'Replies right away · uses the chat budget',
      dot: isT ? deliveries[mode].dot : '#5a8a5e',
      cta: isT ? 'Start thread' : 'Start chat',
      submit: () => this.go(isT ? 'thread' : 'chat', { nw: null }) };
    const archived = [{ name: 'Enclosure v2 print', last: 'Assistant: superseded by v3', when: 'Oct 20' }, { name: 'Pump wiring', last: 'You: done, thanks', when: 'Oct 2' }];
    const extra = { c, pending: s.trayScope === 'all' ? [...threadPend, ...otherPend] : threadPend, tray, trayScopes, trayToggle: () => this.setState({ tray: !s.tray }),
      scrollRef: this.scrollRef, topNav, nw, stop: e => e.stopPropagation(),
      openNewThread: () => this.setState({ nw: 'thread' }), openNewChat: () => this.setState({ nw: 'chat' }),
      themeLabel: theme === 'dark' ? 'Light' : 'Dark', toggleTheme: () => this.setState({ theme: theme === 'dark' ? 'light' : 'dark' }),
      goSettings: () => this.go('settings'), settingsRing: screen === 'settings' ? '#c4673f' : 'transparent', settingsBg: screen === 'settings' ? 'var(--active)' : 'transparent',
      chipOpen: () => this.go(mode === 'working' ? 'run' : 'runs'),
      archToggle: { track: s.archived ? '#c4673f' : 'var(--line3)', knob: s.archived ? '14px' : '2px' },
      archivedRows: s.archived ? archived.map(a => ({ ...a, archived: true, op: '0.7' })) : [] };

    const showEmpty = !!this.props.showEmpty;
    const tabsOf = (list, cur, key) => list.map(([id, label]) => ({ label, color: cur === id ? 'var(--fg)' : 'var(--faint)', line: cur === id ? 'var(--fg)' : 'transparent', open: () => this.setState({ [key]: id }) }));
    const recent = [
      { thread: '#Pump controller firmware', text: 'The second trigger comes from the fallback timer, which re-arms after the DST change. I\'ve planned a fix for the next run.', when: '12 min ago' },
      { thread: '#Flow meter options', text: 'Three options under €30. The YF-B5 fits the existing ¾" line without adapters.', when: 'Nov 3' },
      { thread: '#Receipts to categorize', text: 'Filed 35 of 41. Six are photos of handwritten receipts and need a look from you.', when: 'Wed' },
    ];
    const jobs = showEmpty ? [] : [
      { label: 'Re-index photo library', by: 'run #121', started: 'Wed 03:40', ends: 'expected Sun ~04:00', dni: true, state: '62%', log: '[14:02:11] indexed 18,204 / 29,310\n[14:02:41] thumbnails 61%\n[14:03:12] indexed 18,450 / 29,310' },
      { label: 'Train moisture forecast model', by: 'run #119', started: 'Mon 02:30', ends: 'expected tonight 23:00', dni: false, state: 'epoch 37/50', log: 'epoch 36  loss 0.0412  val 0.0468\nepoch 37  loss 0.0407  val 0.0471' },
    ];
    const pStatus = { Active: ['var(--okInk)', 'var(--okSoft)'], Stale: ['var(--warnInk)', 'var(--warnSoft)'], Archived: ['var(--muted)', 'var(--soft)'] };
    const projAll = [
      { name: 'Garden irrigation', status: 'Active', summary: 'Drip irrigation controller, sensors and schedule. DST fix in progress.', unread: 3, asks: 2, last: '12 min ago' },
      { name: 'Tax prep 2026', status: 'Active', summary: 'Receipts and questions for the accountant. 35 of 41 receipts filed.', unread: 1, asks: 1, last: 'Wed' },
      { name: 'Home lab', status: 'Stale', summary: 'NAS backups and email setup. Nothing new for 9 days.', unread: 0, asks: 0, last: 'Nov 5' },
      { name: 'Kitchen remodel', status: 'Archived', summary: 'Finished in September. Quotes and the final plan are on the shelf.', unread: 0, asks: 0, last: 'Sep 28' },
    ];
    const projList = showEmpty ? [] : projAll.filter(p => s.projFilter === 'All' || p.status === s.projFilter).map(p => ({ ...p, unread: p.unread || false, asks: p.asks || false, sColor: pStatus[p.status][0], sBg: pStatus[p.status][1], op: p.status === 'Archived' ? '0.7' : '1', open: () => this.go('project') }));
    const outc = { Allowed: ['var(--okInk)', 'var(--okSoft)'], Answered: ['var(--infoInk)', 'var(--infoSoft)'], Denied: ['var(--badInk)', 'var(--badSoft)'], Accepted: ['var(--okInk)', 'var(--okSoft)'], Done: ['var(--okInk)', 'var(--okSoft)'], Approved: ['var(--okInk)', 'var(--okSoft)'], Dismissed: ['var(--muted)', 'var(--soft)'] };
    const histAll = [
      { when: 'Today 09:20', kind: 'Permission', text: 'Read access to ~/Documents/receipts', outcome: 'Allowed', where: '#Receipts to categorize', comment: 'Only the 2026 folder, please.' },
      { when: 'Thu', kind: 'Question', text: 'Use the 06:00 or 06:30 slot for the back lawn?', outcome: 'Answered', where: '#Pump controller firmware', comment: '' },
      { when: 'Wed', kind: 'Promote', text: 'Full run to compare flow meters, about 2 pts', outcome: 'Approved', where: '#Flow meter options', comment: 'Stick to ones that ship to Germany.' },
      { when: 'Nov 10', kind: 'Suggestion', text: 'Rename “Misc” to “Home lab”', outcome: 'Accepted', where: '#Home lab', comment: '' },
      { when: 'Nov 8', kind: 'Permission', text: 'Send email from mara@fastmail.com', outcome: 'Denied', where: '#Email setup', comment: 'Drafts only. I send them myself.' },
      { when: 'Nov 6', kind: 'Task', text: 'Photograph the controller wiring', outcome: 'Done', where: '#Pump controller firmware', comment: '' },
    ];
    const hq = s.histQ.toLowerCase();
    const history = histAll.filter(x => !hq || (x.text + x.where + x.comment + x.kind).toLowerCase().includes(hq)).map(x => ({ ...x, comment: x.comment || false, oColor: outc[x.outcome][0], oBg: outc[x.outcome][1] }));
    const D = (children, meta) => ({ dir: true, children, ...(meta || {}) });
    const F = (size, mod, run) => ({ dir: false, size, mod, run });
    const FS = {
      '/workspace': D({
        projects: D({ 'garden-irrigation.md': F('4.1 KB', 'today 02:13', '#123'), 'tax-prep-2026.md': F('2.7 KB', 'Wed', '#121'), 'home-lab.md': F('1.9 KB', 'Nov 5', '#114'), 'kitchen-remodel.md': F('3.3 KB', 'Sep 28', '#87'), 'index.md': F('0.6 KB', 'Nov 2', '#110') }),
        people: D({ 'mara.md': F('1.2 KB', 'Nov 9', '#117'), 'jo.md': F('0.4 KB', 'Oct 29', '#104') }),
        journal: D({ '2026-11': D({ '11-14.md': F('0.9 KB', 'today 02:15', '#123'), '11-12.md': F('1.4 KB', 'Thu', '#120'), '11-10.md': F('1.1 KB', 'Tue', '#119') }), '2026-10': D({ '10-31.md': F('0.8 KB', 'Oct 31', '#108'), '10-24.md': F('1.0 KB', 'Oct 24', '#101') }) }),
        memory: D({ 'preferences.md': F('2.2 KB', 'Nov 9', '#117'), 'standing-instructions.md': F('1.0 KB', 'Oct 20', '—'), 'tools.md': F('1.6 KB', 'Nov 12', '#120') }),
        'index.md': F('0.5 KB', 'Nov 2', '#110'),
      }, { git: true }),
      '/work': D({
        'garden-irrigation': D({
          firmware: D({ 'scheduler.py': F('6.2 KB', 'today 02:12', '#123'), 'test_dst.py': F('3.0 KB', 'today 02:14', '#123'), 'main.py': F('2.2 KB', 'Oct 30', '#106'), 'config.yaml': F('0.8 KB', 'Oct 30', '#106'), out: D({ 'status.json': F('1.1 KB', 'today 02:14', '#123'), 'dashboard.html': F('14 KB', 'Nov 12', '#119'), 'moisture.csv': F('88 KB', 'today 01:00', '#123') }) }, { git: true }),
          enclosure: D({ 'enclosure-v3.stl': F('2.4 MB', 'Nov 10', 'you'), 'enclosure-v3.step': F('5.1 MB', 'Nov 10', 'you') }),
          inbox: D({ 'pump-log-1102.csv': F('18 KB', 'Nov 12', 'you'), 'wiring-diagram.png': F('1.3 MB', 'Oct 28', 'you') }),
        }),
        'tax-prep-2026': D({ receipts: D({ '2026-03-plumber.pdf': F('240 KB', 'Wed', 'you'), '2026-05-laptop.pdf': F('180 KB', 'Wed', 'you'), '2026-08-glasses.jpg': F('2.1 MB', 'Wed', 'you') }), 'summary.csv': F('6 KB', 'Wed', '#121'), 'questions.md': F('1.1 KB', 'Wed', '#121') }),
        'home-lab': D({ 'backup.sh': F('1.8 KB', 'Nov 5', '#114'), 'nas-notes.md': F('2.0 KB', 'Nov 5', '#114') }),
        'kitchen-remodel': D({}),
      }),
    };
    const cwd = s.cwd || '/workspace/projects';
    const rootOf = p => p.startsWith('/work/') || p === '/work' ? '/work' : '/workspace';
    const rootK = rootOf(cwd);
    const segs = cwd.slice(rootK.length).split('/').filter(Boolean);
    let node = FS[rootK], git = !!node.git;
    for (const sg of segs) { node = node.children[sg] || node; if (node.git) git = true; }
    const ext = n => (n.split('.').pop() || '').toLowerCase();
    const names = Object.keys(node.children).sort((a, b) => (node.children[b].dir - node.children[a].dir) || a.localeCompare(b));
    const fsItems = names.map(n => { const c = node.children[n]; const on = !c.dir && s.fileSel === n;
      return { name: c.dir ? n + '/' : n, size: c.dir ? Object.keys(c.children).length + ' items' : c.size, mod: c.dir ? '' : c.mod, run: c.dir ? '' : c.run,
        tag: c.dir ? 'DIR' : ext(n).toUpperCase().slice(0, 4), tagColor: c.dir ? 'var(--accentInk)' : 'var(--muted)', tagBg: c.dir ? 'var(--accentSoft)' : 'var(--soft)', color: c.dir ? 'var(--fg)' : 'var(--fg2)',
        bg: on ? 'var(--rowh)' : 'var(--card)', open: () => c.dir ? this.setState({ cwd: cwd + '/' + n, fileSel: null }) : this.setState({ fileSel: n }) }; });
    const crumbParts = [rootK, ...segs];
    const fsCrumbs = crumbParts.map((p, k) => ({ name: k === 0 ? p : p, sep: k < crumbParts.length - 1 ? '/' : '', color: k === crumbParts.length - 1 ? 'var(--fg)' : 'var(--faint)', go: () => this.setState({ cwd: [rootK, ...segs.slice(0, k)].join('/'), fileSel: null }) }));
    const roots = [['/workspace', 'Notes and memory', true], ['/work', 'Project work', true], ['/shared', 'Shared spaces · coming later', false]].map(([name, desc, on]) => ({ name, desc, label: name.slice(1), bg: rootK === name ? 'var(--fg)' : 'transparent', color: rootK === name ? 'var(--bg)' : 'var(--muted)', shadow: rootK === name ? '0 1px 2px rgba(0,0,0,.15)' : 'none', cursor: on ? 'pointer' : 'not-allowed', op: on ? '1' : '0.5', pick: () => on && this.setState({ cwd: name, fileSel: null }) }));
    const fsOpen = s.fsOpen || {};
    const fsTree = [];
    const walk = (nd, path, depth, shown) => Object.keys(nd.children).sort((a, b) => (nd.children[b].dir - nd.children[a].dir) || a.localeCompare(b)).forEach(n => {
      const c = nd.children[n], p = path + '/' + n, onPath = cwd === p || cwd.startsWith(p + '/');
      const open = c.dir && (fsOpen[p] !== undefined ? fsOpen[p] : onPath);
      const active = c.dir ? (cwd === p && !s.fileSel) : (cwd === path && s.fileSel === n);
      const dot = n.lastIndexOf('.'); fsTree.push({ key: p, rows: shown ? '1fr' : '0fr', op: shown ? 1 : 0, name: c.dir || dot <= 0 ? n : n.slice(0, dot), ext: c.dir || dot <= 0 ? '' : n.slice(dot + 1), pad: (6 + depth * 16) + 'px', caret: c.dir ? (open ? '▾' : '▸') : '', color: c.dir ? 'var(--fg)' : 'var(--fg2)', fw: active ? 600 : c.dir ? 500 : 400, bg: active ? 'var(--active)' : 'transparent',
        open: () => c.dir ? this.setState(st => ({ cwd: p, fileSel: null, fsOpen: { ...(st.fsOpen || {}), [p]: cwd === p ? !open : true } })) : this.setState({ cwd: path, fileSel: n, fsHist: false }) });
      if (c.dir) walk(c, p, depth + 1, shown && open);
    });
    walk(FS[rootK], rootK, 0, true);
    const fsRootDesc = { '/workspace': 'Notes and memory · git', '/work': 'Project work' }[rootK];
    const selF = s.fileSel && node.children[s.fileSel];
    const e = selF ? ext(s.fileSel) : '';
    const mdBodies = { 'garden-irrigation.md': 'ESP32 controller, two pumps, four moisture sensors. Open: the DST double-trigger (run #123) and the flow meter decision.', 'mara.md': 'Prefers short answers. Waters early. Away Dec 20 to Jan 3.', 'preferences.md': 'Units metric. Ask before flashing firmware. Never send email without approval.' };
    const phs = { stl: ['3D viewer', 'drag to orbit · scroll to zoom'], step: ['3D viewer', 'STEP model'], png: ['image preview', ''], jpg: ['image preview', ''], pdf: ['PDF preview', ''], html: ['sandboxed frame', 'agent-made HTML renders on a separate origin'], sh: ['', ''] };
    const view = s.fsView || 'rendered', textual = ['md', 'py', 'yaml', 'sh', 'csv', 'json', 'html'].includes(e), rend = view === 'rendered';
    const rawTexts = {
      md: '# ' + (s.fileSel || '').replace('.md', '') + '\n\n' + ((s.fileSel && ({ 'garden-irrigation.md': 'ESP32 controller, two pumps, four moisture sensors.\n\n## Open\n- DST double-trigger (run #123)\n- Flow meter decision\n\nSee [[projects/home-lab]] and [[people/mara]].' })[s.fileSel]) || 'Notes kept by the assistant.\n\nSee [[projects/home-lab]].'),
      csv: 'time,pump,secs\n11-01 06:00:00,P1,180\n11-01 06:00:04,P1,180\n11-02 06:00:00,P2,120\n11-05 06:00:00,P1,180',
      json: '{"firmware":"0.9.3-rc1","tests":{"passed":5,"total":7},"pumps":[{"id":"P1","last_run":"2026-11-14T06:00:00Z"},{"id":"P2","last_run":"2026-11-13T06:00:00Z"}],"dst_fix":true}',
      html: '<!doctype html>\n<html>\n  <head><title>Moisture</title></head>\n  <body>\n    <div id="chart"></div>\n    <script src="chart.js"><\/script>\n  </body>\n</html>',
    };
    const jsonRows = [['firmware', '"0.9.3-rc1"', 0], ['tests', '{ 2 keys }', 0], ['passed', '5', 1], ['total', '7', 1], ['pumps', '[ 2 items ]', 0], ['0 · id', '"P1"', 1], ['1 · id', '"P2"', 1], ['dst_fix', 'true', 0]].map(([k, v, d]) => ({ k, v, pad: d * 16 + 'px', color: v.startsWith('"') ? 'var(--okInk)' : /^[0-9]|true|false/.test(v) ? 'var(--infoInk)' : 'var(--faint)' }));
    const fs = { crumbs: fsCrumbs, git, items: fsItems, canUp: segs.length > 0, empty: names.length === 0,
      up: () => this.setState({ cwd: [rootK, ...segs.slice(0, -1)].join('/'), fileSel: null }),
      isFolder: !selF, isFile: !!selF, folderName: crumbParts[crumbParts.length - 1],
      summary: names.filter(n => node.children[n].dir).length + ' folders · ' + names.filter(n => !node.children[n].dir).length + ' files',
      history: [{ run: 'run #123', when: 'today 02:12', diff: '+12 −3' }, { run: 'run #119', when: 'Mon 02:41', diff: '+5 −1' }, { run: 'run #114', when: 'Nov 4', diff: '+30' }],
      sel: selF ? { name: s.fileSel, meta: selF.size + ' · ' + selF.mod + (selF.run && selF.run !== '—' ? ' · ' + (selF.run === 'you' ? 'from you' : 'run ' + selF.run) : ''),
        showMd: rend && e === 'md', showCode: (rend || e !== 'md') && ['py', 'yaml', 'sh'].includes(e), showCsv: rend && e === 'csv', showPh: rend && !!phs[e] && e !== 'sh', showJson: rend && e === 'json',
        showRaw: !rend && textual && !['py', 'yaml', 'sh'].includes(e), rawText: rawTexts[e] || '', showBinary: !rend && !textual, binNote: (selF.size || '') + ' · no text view for .' + e + ' files',
        title: s.fileSel.replace('.md', '').replace(/-/g, ' '), body: mdBodies[s.fileSel] || 'Notes kept by the assistant. Links between notes work like a wiki.',
        ph: phs[e] ? phs[e][0] + ' · ' + s.fileSel : '', phHint: phs[e] ? phs[e][1] : '' } : { showMd: false, showCode: false, showCsv: false, showPh: false, showJson: false, showRaw: false, showBinary: false } };
    const egress = [['02:17:41', 'GET', 'api.github.com/repos/mk/garden-fw', '200', '14 KB'], ['02:16:05', 'POST', 'api.anthropic.com/v1/messages', '200', '82 KB'], ['02:15:12', 'GET', 'tracker.example-ads.net/pixel', 'blocked', '0'], ['02:14:30', 'GET', 'pypi.org/simple/pytz/', '200', '31 KB'], ['02:12:02', 'POST', 'pypi.org/legacy/', 'blocked', '0'], ['02:09:44', 'GET', 'api.open-meteo.com/v1/forecast', '200', '6 KB'], ['02:03:10', 'PUB', 'garden-broker.local:1883', 'ok', '0.2 KB'], ['02:00:12', 'GET', 'api.github.com/user', '200', '2 KB']].map(([t, m, host, st, b]) => ({ t, m, host, s: st, b, bg: st === 'blocked' ? 'var(--badSoft)' : 'var(--card)', color: st === 'blocked' ? 'var(--badInk)' : 'var(--fg2)' }));
    const topHosts = [['api.github.com', 212], ['garden-broker.local', 96], ['api.open-meteo.com', 48], ['pypi.org', 31]].map(([host, n]) => ({ host, n, w: Math.round(n / 212 * 100) + '%' }));
    const netRules2 = [['Allow', 'var(--okInk)', '*.github.com', ''], ['Allow read-only', 'var(--infoInk)', 'pypi.org', 'Added by approving a permission · Nov 12'], ['Allow', 'var(--okInk)', 'api.open-meteo.com', ''], ['Allow', 'var(--okInk)', 'garden-broker.local', ''], ['Block', 'var(--badInk)', '*', 'Everything else']].map(([rule, color, host, note]) => ({ rule, color, host, note: note || false }));
    const palAll = [
      ['Actions', 'New thread in General', '', '', () => this.go('new', { palette: false, homeDest: 'general' })],
      ['Actions', 'New chat', '', 'C', () => this.go('new', { palette: false, homeDest: 'chat' })],
      ['Actions', 'Reply now in #Pump controller firmware', '1 pt · 14 of 20 reply pts left', '⌘↵', () => this.go('thread', { palette: false })],
      ['Actions', 'Pause the assistant', 'Skips planned wakeups until you resume', '', () => this.setState({ palette: false })],
      ['Actions', 'Toggle dark mode', '', '', () => this.setState({ palette: false, theme: this.theme() === 'dark' ? 'light' : 'dark' })],
      ['Threads', 'Pump controller firmware', 'Garden irrigation', '', () => this.go('thread', { palette: false, thread: 'Pump controller firmware' })],
      ['Threads', 'Moisture sensor calibration', 'Garden irrigation · 2 unread', '', () => this.go('thread', { palette: false, thread: 'Moisture sensor calibration' })],
      ['Threads', 'Receipts to categorize', 'Tax prep 2026', '', () => this.go('thread', { palette: false, thread: 'Receipts to categorize' })],
      ['Messages', '…the fallback timer re-arms after the DST change…', '#Pump controller firmware · 12 min ago', '', () => this.go('thread', { palette: false })],
      ['Messages', '…the YF-B5 fits the existing ¾" line without adapters…', '#Flow meter options · Nov 3', '', () => this.go('thread', { palette: false })],
      ['Projects', 'Garden irrigation', 'Active · 2 open asks', '', () => this.go('project', { palette: false })],
      ['Projects', 'Tax prep 2026', 'Active', '', () => this.go('project', { palette: false })],
      ['Files', 'scheduler.py', '/work/garden-irrigation/firmware', '', () => this.go('files', { palette: false, cwd: '/work/garden-irrigation/firmware', fileSel: 'scheduler.py' })],
      ['Files', 'garden-irrigation.md', '/workspace/projects', '', () => this.go('files', { palette: false, cwd: '/workspace/projects', fileSel: 'garden-irrigation.md' })],
      ['Runs', '#123 Fix the Sunday double pump trigger', 'Running · 3.8 / 10 pts', '', () => this.go('run', { palette: false })],
      ['Runs', '#121 Categorize 41 receipts', 'Partial · Wed', '', () => this.go('run', { palette: false })],
    ];
    const pq = s.palQ.toLowerCase();
    const palHits = palAll.filter(([g, t, sub]) => (s.palKind === 'All' || s.palKind === g) && (!pq || (t + ' ' + sub).toLowerCase().includes(pq)));
    const palGroups = [...new Set(palHits.map(x => x[0]))].map(g => ({ label: g, items: palHits.filter(x => x[0] === g).map(([, title, sub, key, run]) => ({ title, sub: sub || false, key: key || false, run, bg: palHits[0][1] === title ? 'var(--side)' : 'transparent' })) }));
    const pal = { op: s.palette ? 1 : 0, vis: s.palette ? 'visible' : 'hidden', tf: s.palette ? 'none' : 'translateY(10px) scale(.97)', open: s.palette, q: s.palQ, empty: palHits.length === 0, groups: palGroups, close: () => this.setState({ palette: false }),
      change: e => this.setState({ palQ: e.target.value }),
      key: e => { if (e.key === 'Enter' && palHits[0]) palHits[0][4](); },
      kinds: ['All', 'Actions', 'Threads', 'Messages', 'Projects', 'Files', 'Runs'].map(k => ({ label: k, ...pill(s.palKind === k), pick: () => this.setState({ palKind: k }) })) };
    const login = { step1: s.loginStep === 1, step2: s.loginStep === 2, locked: s.locked, sub: s.loginStep === 1 ? 'Sign in to your assistant' : 'Enter the code from your authenticator app', next: () => this.setState({ loginStep: 2 }), lock: () => this.setState({ locked: !s.locked }), done: () => this.go('home', { loginStep: 1 }) };
    const stepDefs = [['Owner account', 'Create the owner', 'This is the only account on this server. You can add passkeys and 2FA later in Settings.'], ['Timezone', 'Your timezone', 'Wakeups, protected hours and quiet hours use this.'], ['Provider', 'Connect a model provider', 'The login runs through the server, so your keys stay on it. Connect at least one.'], ['Level', 'Pick a level', 'How much the assistant may spend each week. You can change it any time.'], ['Hub', 'Join a hub', 'Hubs connect assistants with friends and shared services.'], ['Self-test', 'Self-test', 'Everything checks out. Your assistant will plan its first wakeup after you open it.']];
    const ss = s.setupStep;
    const setup = { title: stepDefs[ss][1], text: stepDefs[ss][2], canBack: ss > 0, back: () => this.setState({ setupStep: ss - 1 }), cta: ss === 5 ? 'Open l0wagent' : ss === 4 ? 'Skip for now' : 'Continue', next: () => ss === 5 ? this.go('home', { setupStep: 0 }) : this.setState({ setupStep: ss + 1 }),
      steps: stepDefs.map(([label], i) => ({ label, bg: i === ss ? 'var(--active)' : 'transparent', color: i <= ss ? 'var(--fg)' : 'var(--faint)', dotBg: i < ss ? 'var(--okInk)' : i === ss ? 'var(--fg)' : 'var(--track)', dotColor: i <= ss ? 'var(--bg)' : 'var(--faint)', mark: i < ss ? '✓' : String(i + 1), pick: () => this.setState({ setupStep: i }) })),
      providers: [['Anthropic', 'Claude models · log in with your account'], ['OpenAI', 'GPT models · log in or paste a key']].map(([name, note]) => { const on = !!s.prov[name]; return { name, note: on ? 'Connected · usage from provider' : note, cta: on ? '✓ Connected' : 'Log in', color: on ? 'var(--okInk)' : 'var(--fg)', connect: () => this.setState(st => ({ prov: { ...st.prov, [name]: true } })) }; }) };
    for (let i = 0; i < 6; i++) setup['s' + i] = ss === i;
    const selfTest = [['Database and storage', 'ok'], ['Agent computer reachable', 'ok'], ['Provider login', 'Anthropic'], ['Network rules loaded', '4 rules'], ['Push notifications', 'needs HTTPS · ntfy fallback']].map(([label, note], i) => ({ label, note, mark: i === 4 ? '!' : '✓', color: i === 4 ? 'var(--warnInk)' : 'var(--okInk)' }));
    const waitingCount = showEmpty ? 0 : 7;
    const extra2 = { recent, jobs, jobsEmpty: jobs.length === 0, projList, projListEmpty: projList.length === 0,
      projFilters: ['All', 'Active', 'Stale', 'Archived'].map(f => ({ label: f, ...pill(s.projFilter === f), pick: () => this.setState({ projFilter: f }) })),
      inboxTabs: tabsOf([['waiting', 'Waiting · ' + waitingCount], ['history', 'History']], s.inboxTab, 'inboxTab'),
      ib: { waiting: s.inboxTab === 'waiting' && !showEmpty, waitingEmpty: s.inboxTab === 'waiting' && showEmpty, history: s.inboxTab === 'history' },
      inboxCountLabel: waitingCount ? waitingCount + ' waiting on you' : 'All caught up',
      history, historyEmpty: history.length === 0, histQ: s.histQ, histChange: e => this.setState({ histQ: e.target.value }),
      runsTabs: tabsOf([['runs', 'Runs'], ['jobs', 'Long jobs · ' + jobs.length]], s.runsTab, 'runsTab'), rt: { runs: s.runsTab === 'runs', jobs: s.runsTab === 'jobs' },
      roots, fs, fsTree, fsRootDesc, fileHistory: [{ run: 'run #123', when: 'today 02:12', diff: '+12 −3' }, { run: 'run #119', when: 'Mon 02:41', diff: '+5 −1' }, { run: 'run #114', when: 'Nov 4', diff: '+30' }],
      egress, topHosts, netRules2, netFilters: ['All', 'Blocked', 'GET', 'POST', 'Last hour'].map((l, i) => ({ label: l, ...pill(i === 0), pick: () => {} })),
      pal, palRef: this.palRef, openPalette: () => this.setState({ palette: true, palQ: '' }),
      login, setup, selfTest,
      banner: !s.bannerOff && this.props.showUpdate !== false, closeBanner: () => this.setState({ bannerOff: true }),
      goHome: () => this.go('home'), homeBg: sel(screen === 'home'), goProjects: () => this.go('projects'), projectsBg: sel(screen === 'projects'),
      goInbox: () => this.go('inbox'), goCalendar: () => this.go('calendar'), goJobs: () => this.go('runs', { runsTab: 'jobs' }), goWeb: () => this.go('web'), goNetwork: () => this.go('network') };

    const kindDot = { Question: 'var(--infoInk)', 'Pick one': 'var(--infoInk)', 'Pick any': 'var(--infoInk)', Written: 'var(--infoInk)', Permission: '#b8862f', Suggestion: 'var(--faint2)', Promote: '#c4673f', 'Secure input': 'var(--okInk)', Task: 'var(--infoInk)', Security: 'var(--badInk)' };
    const wItems = (s.trayScope === 'all' ? [...threadPend, ...otherPend] : threadPend).map(p => ({ ...p, dot: kindDot[p.kind] || 'var(--faint2)', bg: 'transparent' }));
    const doneList = pend.filter(([id]) => s.ans[id]).map(([id, kind, text]) => ({ text, jump: () => this.jump(id) }));
    const wait = { open: s.waitOpen, showBtn: !s.waitOpen, toggle: () => this.setState({ waitOpen: !s.waitOpen, panel: false }),
      btnLabel: threadPend.length ? threadPend.length + ' waiting on you' : 'Nothing waiting', dot: threadPend.length ? '#c4673f' : 'var(--faint2)',
      count: threadPend.length || '', items: wItems, empty: wItems.length === 0, done: doneList, hasDone: doneList.length > 0, doneCount: doneList.length,
      scopes: [['thread', 'This thread · ' + threadPend.length], ['all', 'Everywhere · ' + (threadPend.length + otherPend.length)]].map(([k, l]) => ({ label: l, bg: s.trayScope === k ? 'var(--card)' : 'transparent', pick: () => this.setState({ trayScope: k }) })) };
    const dest = s.homeDest;
    const home = { isChat: dest === 'chat', chatBg: dest === 'chat' ? 'var(--card)' : 'transparent', genBg: dest === 'general' ? 'var(--card)' : 'transparent',
      pickChat: () => this.setState({ homeDest: 'chat' }), pickGen: () => this.setState({ homeDest: 'general' }),
      text: s.homeText, change: e => this.setState({ homeText: e.target.value }),
      placeholder: dest === 'chat' ? 'Start a chat. Replies stream right away.' : 'Write to the assistant. It picks this up in its General thread.',
      note: dest === 'chat' ? 'New 1:1 chat · replies right away · uses the chat budget' : 'New thread in General · ' + deliveries[mode].text,
      dot: dest === 'chat' ? '#5a8a5e' : deliveries[mode].dot,
      send: () => this.go(dest === 'chat' ? 'chat' : 'thread', dest === 'chat' ? { homeText: '' } : { homeText: '', thread: 'General' }) };
    const chatIn = screen === 'chat';
    const chatGroups = [
      ['Today', [['Setting up the Fastmail MCP', '2 h'], ['Is 0.8 bar enough for drip lines?', '5 h']]],
      ['Past week', [['Convert feed rates to metric', 'Wed'], ['Which flow meter fits ¾" pipe?', 'Tue'], ['Scaling the sourdough recipe', 'Mon'], ['Draft a note to the landlord', 'Mon'], ['What does EXIF orientation do?', 'Sun']]],
      ['Earlier', [['Explain my router logs', 'Nov 4'], ['Birthday gift ideas for Jo', 'Oct 29'], ['Compare two NAS drives', 'Oct 26'], ['Summarize the HOA minutes', 'Oct 21'], ['Python: dataclass vs TypedDict', 'Oct 18'], ['Recipe for leftover rice', 'Oct 12'], ['Translate a letter to German', 'Oct 3']]],
    ].map(([label, items]) => ({ label, items: items.map(([name, meta], i) => ({ name, meta, bg: sel(chatIn && label === 'Today' && i === 0), open: () => this.go('chat') })) }));
    const extra3 = { wait, home, chatGroups, sideChats: chatIn, sideAssistant: !chatIn,
      sideNew: { label: 'New', key: 'N', open: () => this.go('new', s.homeText.trim() ? {} : { homeDest: 'chat' }) } };

    const conv = ['thread', 'project', 'projects', 'chat', 'new'].includes(screen);
    const extra4 = {
      side: { w: conv && !s.sideCollapsed ? '260px' : '0px', op: conv && !s.sideCollapsed ? 1 : 0, show: conv && !s.sideCollapsed, rail: conv && s.sideCollapsed, collapse: () => this.setState({ sideCollapsed: true }), expand: () => this.setState({ sideCollapsed: false }),
        },
      tfiles: { w: s.filesPanel ? '310px' : '0px', op: s.filesPanel ? 1 : 0, open: s.filesPanel, toggle: () => this.setState({ filesPanel: !s.filesPanel, panel: false }), btnBg: s.filesPanel ? 'var(--active)' : 'var(--card)', openProject: () => this.go('project', { projTab: 'files' }) },
      changeSummary: changes.map(c => { const a = c.items.reduce((n, i) => n + (parseInt((i.add || '0').replace('+', '')) || 0), 0), d = c.items.reduce((n, i) => n + (parseInt((i.del || '0').replace('−', '')) || 0), 0); return { group: c.group, detail: c.items.length + (c.items.length === 1 ? ' item' : ' items') + (a || d ? '  +' + a + ' −' + d : '') }; }),
      nt: { yes: screen === 'thread' && s.thread === 'New thread', no: !(screen === 'thread' && s.thread === 'New thread'), project: s.ntProj || 'General' },
      newGeneral: (e) => { e.stopPropagation(); this.go('new', { homeDest: 'general' }); },
      newChat: () => this.go('new', { homeDest: 'chat' }),
      fsViews: [['rendered', 'Rendered'], ['raw', 'Raw']].map(([k, l]) => ({ label: l, bg: (s.fsView || 'rendered') === k ? 'var(--card)' : 'transparent', pick: () => this.setState({ fsView: k }) })),
      fsHist: { op: s.fsHist ? 1 : 0, vis: s.fsHist ? 'visible' : 'hidden', tf: s.fsHist ? 'none' : 'translateY(-4px) scale(.98)', open: !!s.fsHist, bg: s.fsHist ? 'var(--active)' : 'var(--card)', toggle: () => this.setState({ fsHist: !s.fsHist }) },
      more: (() => { const items = [['Calendar', 'calendar', 'Wakeups, routines, protected hours'], ['Network', 'network', 'Request log and egress rules'], ['Web page', 'web', "The assistant's web port"]]; const cur = items.find(i => i[1] === screen);
        return { op: s.moreOpen ? 1 : 0, vis: s.moreOpen ? 'visible' : 'hidden', tf: s.moreOpen ? 'none' : 'translateY(-4px) scale(.98)', disp: s.moreOpen ? 'block' : 'none', open: !!s.moreOpen, toggle: () => this.setState({ moreOpen: !s.moreOpen }), label: cur ? cur[0] : 'More', color: cur ? 'var(--fg)' : 'var(--muted)', bg: cur ? 'var(--active)' : 'transparent',
          items: items.map(([label, k, desc]) => ({ label, desc, bg: screen === k ? 'var(--active)' : 'transparent', open: () => this.go(k, { moreOpen: false }) })) }; })(),
      csvPanel: { w: s.panel ? '400px' : '0px', op: s.panel ? 1 : 0 },
      fsKey: (s.cwd || '') + '|' + (s.fileSel || '') + '|' + (s.fsView || ''), threadKey: s.thread,
      bannerA: { rows: (!s.bannerOff && this.props.showUpdate !== false) ? '1fr' : '0fr', op: (!s.bannerOff && this.props.showUpdate !== false) ? 1 : 0 },
      homeBtn: { color: screen === 'home' ? 'var(--fg)' : 'var(--muted)', bg: screen === 'home' ? 'var(--active)' : 'transparent' },
    };
    return {
      ...extra,
      ...extra2,
      ...extra3,
      ...extra4,
      crumb: crumbs[screen], is, chip: chips[mode], delivery: deliveries[mode],
      projects, chats, footer, generalBg: sel(false),
      archivedLabel: s.archived ? 'Hide archived' : 'Show archived',
      toggleArchived: () => this.setState({ archived: !s.archived }),
      goGeneral: () => this.go('thread', { thread: 'General' }), goThread,
      threadTitle: s.thread,
      openLog: () => this.setState({ panel: true, filesPanel: false }), closePanel: () => this.setState({ panel: false }), panelOpen: s.panel,
      openRun: () => this.go('run'),
      csvRows: [['time', 'pump', 'secs'], ['11-01 06:00:00', 'P1', '180'], ['11-01 06:00:04', 'P1', '180'], ['11-02 06:00:00', 'P2', '120'], ['11-05 06:00:00', 'P1', '180'], ['11-08 06:00:00', 'P1', '180'], ['11-08 06:00:04', 'P1', '180'], ['11-09 06:00:00', 'P2', '120']].map((r, i) => ({ a: r[0], b: r[1], c: r[2], bg: i === 0 ? 'var(--rowh)' : (r[0].endsWith(':04') ? 'var(--badSoft)' : 'var(--card)') })),
      q1Open: !s.q1, q1Done: !!s.q1, q1Answer: s.q1,
      q1a: () => this.setState({ q1: 'Flash it' }), q1b: () => this.setState({ q1: 'Leave it for me' }),
      sugOpen: !s.sug, sugDone: !!s.sug, sugResult: s.sug === 'a' ? 'Accepted · thread created' : 'Dismissed',
      sugAccept: () => this.setState({ sug: 'a' }), sugDismiss: () => this.setState({ sug: 'd' }),
      projTabs: tabRow(pTabs, s.projTab, 'projTab'), pt: flags(pTabs, s.projTab),
      projThreads: [...[
        { name: 'Pump controller firmware', last: 'Assistant: Planned in run #123, switch fallback timer to UTC…', when: '12 min ago', archived: false, op: '1' },
        { name: 'Moisture sensor calibration', last: 'You: sensor 3 reads 8% high after rain', when: 'Nov 12', archived: false, op: '1' },
        { name: 'Winter shutdown checklist', last: 'Assistant: drained lines, 2 of 5 steps left', when: 'Nov 8', archived: false, op: '1' },
        { name: 'Flow meter options', last: 'Assistant: three options under €30, comparison attached', when: 'Nov 3', archived: false, op: '1' },
      ], ...extra.archivedRows],
      files, file, codeText: 'DEBOUNCE_S = 600\n\ndef should_fire(slot, now):\n    now_utc = now.astimezone(timezone.utc)\n    if abs((now_utc - slot.utc).total_seconds()) < DEBOUNCE_S:\n        return not slot.fired_recently(now_utc)\n    return False',
      projAsks: [{ kind: 'Question', text: 'Should I flash the fix to the controller myself once tests pass?' }, { kind: 'Permission', text: 'Write access to ~/garden/firmware/ for this project' }],
      projActivity: [
        { when: '12 min ago', kind: 'Claim', text: 'Run #123 planned: switch fallback timer to UTC' },
        { when: '14 min ago', kind: 'Shelved', text: 'scheduler.py added as a live item' },
        { when: '1 h ago', kind: 'Note', text: 'Project doc updated: current state, open decisions' },
        { when: 'Nov 12', kind: 'Run', text: 'Run #119 traced the double trigger (done, 5.5 pts)' },
        { when: 'Nov 10', kind: 'Upload', text: 'enclosure-v3.stl from you' },
      ],
      inboxFilters: iFilters.map(f => ({ label: f, ...pill(f === s.inboxFilter), pick: () => this.setState({ inboxFilter: f }) })), inboxGroups,
      days, legend, hours: [0, 3, 6, 9, 12, 15, 18, 21].map(h => ({ label: String(h).padStart(2, '0'), top: (h * H + 2) + 'px' })),
      scheduled: [
        { when: 'Sat 02:00', budget: '10 pts', reason: 'Finish the pump fix, then receipts', model: 'Sonnet · medium' },
        { when: 'Sun 06:00', budget: '3 pts', reason: 'Weekly digest', model: 'Haiku · low' },
        { when: 'Sun 23:00', budget: '2 pts', reason: 'Fallback if nothing else ran', model: 'Haiku · low' },
      ],
      runFilters: ['All kinds', 'Planned', 'Routine', 'Sweep', 'Promoted', 'All projects ▾'].map((l, i) => ({ label: l, ...pill(i === 0) })),
      runs,
      rm: { simple: s.runMode === 'simple', detailed: s.runMode === 'detailed', simpleBg: s.runMode === 'simple' ? 'var(--card)' : 'transparent', detailBg: s.runMode === 'detailed' ? 'var(--card)' : 'transparent' },
      runSimple: () => this.setState({ runMode: 'simple' }), runDetailed: () => this.setState({ runMode: 'detailed' }),
      runMeta: [{ k: 'Why it woke', v: 'Planned · Sat 02:00' }, { k: 'Level', v: 'Steady' }, { k: 'Reviewer', v: 'Every 10 min' }, { k: 'Model', v: 'Sonnet · medium → high' }],
      claims, statusTimeline, changes,
      detailFilters: ['All', 'Tools', 'Diffs', 'Reviewer', 'Control'].map((l, i) => ({ label: l, ...pill(i === 0) })),
      detailItems, thinking: { track: s.thinking ? '#c4673f' : 'var(--line3)', knob: s.thinking ? '14px' : '2px' }, toggleThinking: () => this.setState({ thinking: !s.thinking }),
      toolTabs: tabRow(tTabs, s.toolsTab, 'toolsTab'), tt: flags(tTabs, s.toolsTab),
      secrets: [
        { name: 'MQTT_PASSWORD', desc: 'garden-broker.local', scope: 'Garden irrigation', last: '12 min ago', reads: 14 },
        { name: 'FASTMAIL_APP_PW', desc: 'Read-only app password', scope: 'Fastmail MCP', last: '2 h ago', reads: 3 },
        { name: 'GITHUB_TOKEN', desc: 'Fine-grained, 2 repos', scope: 'All projects', last: 'Wed', reads: 41 },
        { name: 'NAS_SSH_KEY', desc: 'Backup user on nas.lan', scope: 'Home lab', last: 'Nov 2', reads: 6 },
      ],
      integrations: [
        { name: 'Fastmail', status: 'Connected · preset', dot: '#5a8a5e', tools: [{ name: 'search_mail', ...pol('a') }, { name: 'read_message', ...pol('a') }, { name: 'send_message', ...pol('b') }] },
        { name: 'GitHub', status: 'Connected', dot: '#5a8a5e', tools: [{ name: 'read_repo', ...pol('a') }, { name: 'open_pull_request', ...pol('k') }] },
        { name: 'Home Assistant', status: 'Error · token expired', dot: '#b8862f', tools: [{ name: 'get_state', ...pol('a') }, { name: 'call_service', ...pol('k') }] },
      ],
      netRules: [
        { rule: 'Allow', color: 'var(--okInk)', host: '*.github.com', hits: '212 requests · 7 d' },
        { rule: 'Allow', color: 'var(--okInk)', host: 'api.open-meteo.com', hits: '48 requests · 7 d' },
        { rule: 'Allow', color: 'var(--okInk)', host: 'garden-broker.local:1883', hits: '96 requests · 7 d' },
        { rule: 'Block', color: 'var(--badInk)', host: '*', hits: '3 blocked · 7 d' },
      ],
      apps: [{ name: 'Telegram bot', scopes: 'Post to threads, read replies' }, { name: 'Claude.ai connector', scopes: 'Read projects, ask the assistant' }],
      setTabs: setTabList.map(t => ({ label: t, bg: sel(s.setTab === t), open: () => this.setState({ setTab: t }) })),
      st: { budgets: s.setTab === 'Budgets & levels', roles: s.setTab === 'Roles', appearance: s.setTab === 'Appearance', other: !['Budgets & levels', 'Roles', 'Appearance'].includes(s.setTab) },
      themeOpts: [['light', 'Light', 'Warm off-white'], ['dark', 'Dark', 'Warm charcoal'], ['system', 'Match system', 'Follows your OS setting']].map(([k, label, desc]) => { const cur = s.themePref || this.props.theme || 'light'; const on = cur === k; return { label, desc, border: on ? '#c4673f' : 'var(--line)', ring: on ? '#c4673f' : 'var(--dim)', dot: on ? '#c4673f' : 'transparent', pick: () => this.setState({ themePref: k, theme: k === 'system' ? (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : k }) }; }), setLabel: s.setTab,
      levels, levelText: levelDefs[s.level][1],
      allowances: [{ name: 'Anthropic', used: '46%', reserve: '15%', text: '46% used · 15% reserved' }, { name: 'OpenAI', used: '12%', reserve: '20%', text: '12% used · 20% reserved' }],
      budgetFields: [
        { k: 'Allowed hours', hint: 'when wakeups may run', v: '00:00 – 07:00, 12:00 – 14:00' },
        { k: 'Wakeup limits', hint: 'per day', v: '4 max' },
        { k: 'Reply budget', hint: 'for “Reply now”', v: '20 pts / wk' },
        { k: 'Fallback', hint: 'if nothing ran for', v: '48 h · 2 pts' },
        { k: 'Sweep', hint: 'tidy threads', v: 'Tue, Thu, Sat 13:00' },
        { k: 'Protected hours', hint: 'reserved for you', v: '18:00 – 22:00' },
      ],
      roles: [
        { role: 'Worker', provider: 'Anthropic', model: 'claude-sonnet', effort: 'medium', range: 'haiku – opus, low – high' },
        { role: 'Reviewer', provider: 'OpenAI', model: 'gpt-mini', effort: 'low', range: 'low – medium' },
        { role: 'Replier', provider: 'Anthropic', model: 'claude-haiku', effort: 'low', range: 'haiku – sonnet' },
        { role: 'Chat', provider: 'Anthropic', model: 'claude-sonnet', effort: 'medium', range: 'haiku, sonnet' },
        { role: 'Utility', provider: 'Anthropic', model: 'claude-haiku', effort: 'low', range: 'haiku only' },
      ],
    };
  }
}
