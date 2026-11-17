// Plant log's UI: plain custom elements, no build step. The app imports this module once and creates the
// elements the manifest names. Before attaching one it sets two properties:
//   el.l0w      the host: { ext, api(path, {method, body}), on(type, fn) -> off, navigate(to), theme }
//   el.context  what the slot is about (a project tab gets { project }, an Inbox card gets { card, resolve }, …)
// Style with the app's theme tokens (var(--fg), var(--line), …): they inherit into shadow roots, so light and
// dark both work without extra code.

const css = `
  :host { display: block; font: 14px/1.5 var(--sans); color: var(--fg); }
  h2 { font: 500 22px/1.2 var(--serif); margin: 0 0 14px; }
  table { width: 100%; border-collapse: collapse; }
  td, th { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  .due { color: var(--warnInk); background: var(--warnSoft); border-radius: 999px; padding: 1px 8px; font-size: 12px; }
  .muted { color: var(--muted); }
  button { font: inherit; border: 1px solid var(--line2); background: var(--card); color: var(--fg); border-radius: 7px; padding: 3px 10px; cursor: pointer; }
  button:hover { background: var(--hover); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  form { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
  input { font: inherit; border: 1px solid var(--line2); background: var(--card); color: var(--fg); border-radius: 7px; padding: 4px 8px; }
  li { padding: 3px 0; }
  ul { margin: 0; padding-left: 0; list-style: none; }
`;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const when = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : 'never');

// A base element: shadow root, the host's live events, and re-render on change.
class Base extends HTMLElement {
  connectedCallback() {
    this.root ??= this.attachShadow({ mode: 'open' });
    this.off = this.l0w.on('plants_changed', () => this.load());
    this.load();
  }
  disconnectedCallback() { this.off?.(); }
  async load() {
    try { this.plants = await this.l0w.api('/plants'); this.error = null; }
    catch (e) { this.error = e.message; }
    this.render();
  }
  draw(html) { this.root.innerHTML = `<style>${css}</style>${this.error ? `<p class="muted">${esc(this.error)}</p>` : html}`; }
}

class PlantPage extends Base {
  render() {
    const rows = (this.plants ?? []).map((p) => `
      <tr><td>${esc(p.name)} ${p.due ? '<span class="due">due</span>' : ''}</td>
          <td class="muted">${when(p.last_watered)}</td>
          <td class="muted">${esc(p.notes?.at(-1)?.note ?? '')}</td>
          <td><button data-water="${esc(p.name)}">Watered</button></td></tr>`).join('');
    this.draw(`<h2>Plants</h2>
      <table><tr><th>Plant</th><th>Last watered</th><th>Latest note</th><th></th></tr>${rows || '<tr><td colspan="4" class="muted">No plants yet.</td></tr>'}</table>
      <form><input name="plant" placeholder="Plant" required><input name="note" placeholder="Note" size="30"><button class="primary">Add note</button></form>`);
    this.root.querySelectorAll('[data-water]').forEach((b) => b.addEventListener('click', async () => {
      await this.l0w.api(`/plants/${encodeURIComponent(b.dataset.water)}/water`, { method: 'POST' });
    }));
    this.root.querySelector('form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      await this.l0w.api('/plants', { method: 'POST', body: { plant: f.get('plant'), note: f.get('note') } });
    });
  }
}

class PlantSidebar extends Base {
  render() {
    const due = (this.plants ?? []).filter((p) => p.due);
    this.draw(`<ul>${due.map((p) => `<li>${esc(p.name)} <span class="due">due</span></li>`).join('') || '<li class="muted">Nothing due</li>'}</ul>`);
  }
}

class PlantTab extends Base {
  // A project tab: context.project is the project's slug. This example shows the same log for every project.
  render() {
    const notes = (this.plants ?? []).flatMap((p) => (p.notes ?? []).map((n) => ({ ...n, plant: p.name }))).sort((a, b) => b.at.localeCompare(a.at));
    this.draw(`<ul>${notes.slice(0, 30).map((n) => `<li><b>${esc(n.plant)}</b> <span class="muted">${when(n.at)} · ${esc(n.by)}</span><br>${esc(n.note)}</li>`).join('') || '<li class="muted">No notes yet.</li>'}</ul>`);
  }
}

class WaterCard extends HTMLElement {
  // An Inbox card: context.card is the card ({ id, kind, title, data, status }), context.resolve(action, data) closes it.
  connectedCallback() {
    this.root ??= this.attachShadow({ mode: 'open' });
    const { card, resolve } = this.context;
    this.root.innerHTML = `<style>${css}</style>
      <div>${esc(card.title)} <span class="muted">· last watered ${when(card.data.last_watered)}</span></div>
      ${card.status === 'open' ? '<form><button class="primary" data-a="watered">Watered</button><button data-a="skip">Skip</button></form>' : `<div class="muted">${esc(card.resolution?.action ?? card.status)}</div>`}`;
    this.root.querySelectorAll('[data-a]').forEach((b) => b.addEventListener('click', (e) => { e.preventDefault(); resolve(b.dataset.a); }));
  }
}

class PlantPreview extends HTMLElement {
  // A file previewer: context.url serves the file's bytes, context.path is its path.
  async connectedCallback() {
    this.root ??= this.attachShadow({ mode: 'open' });
    const text = await (await fetch(this.context.url, { credentials: 'include' })).text();
    this.root.innerHTML = `<style>${css}</style><h2>${esc(this.context.path.split('/').pop())}</h2><pre>${esc(text)}</pre>`;
  }
}

const define = (name, cls) => customElements.get(name) || customElements.define(name, cls);
define('plantlog-page', PlantPage);
define('plantlog-sidebar', PlantSidebar);
define('plantlog-tab', PlantTab);
define('plantlog-water-card', WaterCard);
define('plantlog-preview', PlantPreview);
