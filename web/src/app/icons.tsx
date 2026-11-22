import type { ReactNode } from 'react'

/** The phone navigation's line icons: 1.8 stroke, round caps, like the header's own SVGs. */
const svg = (size: number, children: ReactNode) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
)

export const Icon = {
  home: (s = 22) => svg(s, <path d="M3.5 10.5 12 3.5l8.5 7V20a1 1 0 0 1-1 1H15v-6.5H9V21H4.5a1 1 0 0 1-1-1z" />),
  chats: (s = 22) => svg(s, <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H10l-4.5 4v-4h0A1.5 1.5 0 0 1 4 14.5z" />),
  inbox: (s = 22) => svg(s, <><path d="M3.5 13.5 6 5.5A1.5 1.5 0 0 1 7.4 4.5h9.2A1.5 1.5 0 0 1 18 5.5l2.5 8" /><path d="M3.5 13.5V19a1 1 0 0 0 1 1h15a1 1 0 0 0 1-1v-5.5H15a3 3 0 0 1-6 0z" /></>),
  runs: (s = 22) => svg(s, <><circle cx="12" cy="12" r="8.5" /><path d="M10 8.6v6.8l5.6-3.4z" /></>),
  more: (s = 22) => svg(s, <><circle cx="5.5" cy="12" r="1.2" fill="currentColor" /><circle cx="12" cy="12" r="1.2" fill="currentColor" /><circle cx="18.5" cy="12" r="1.2" fill="currentColor" /></>),
  menu: (s = 20) => svg(s, <><line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="17" x2="14" y2="17" /></>),
  search: (s = 19) => svg(s, <><circle cx="11" cy="11" r="6.5" /><line x1="16" y1="16" x2="20.5" y2="20.5" /></>),
  plus: (s = 20) => svg(s, <><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></>),
  close: (s = 18) => svg(s, <><line x1="6" y1="6" x2="18" y2="18" /><line x1="18" y1="6" x2="6" y2="18" /></>),
  files: (s = 20) => svg(s, <path d="M3.5 6.5A1.5 1.5 0 0 1 5 5h4.2l2 2.5H19A1.5 1.5 0 0 1 20.5 9v9.5A1.5 1.5 0 0 1 19 20H5a1.5 1.5 0 0 1-1.5-1.5z" />),
  tools: (s = 20) => svg(s, <path d="M14.5 6.5a4 4 0 0 0 4.9 4.9L20 12l-8 8a2.1 2.1 0 0 1-3-3l8-8z M14.5 6.5 17 4l3 3-2.5 2.5" />),
  calendar: (s = 20) => svg(s, <><rect x="3.5" y="5" width="17" height="15" rx="2" /><line x1="3.5" y1="10" x2="20.5" y2="10" /><line x1="8" y1="3" x2="8" y2="6.5" /><line x1="16" y1="3" x2="16" y2="6.5" /></>),
  network: (s = 20) => svg(s, <><circle cx="12" cy="12" r="8.5" /><path d="M3.5 12h17M12 3.5c2.6 2.4 3.8 5.2 3.8 8.5s-1.2 6.1-3.8 8.5c-2.6-2.4-3.8-5.2-3.8-8.5S9.4 5.9 12 3.5z" /></>),
  friends: (s = 20) => svg(s, <><circle cx="9" cy="8.5" r="3.2" /><path d="M3.5 19.5c0-3.2 2.5-5.4 5.5-5.4s5.5 2.2 5.5 5.4" /><circle cx="16.8" cy="9.5" r="2.5" /><path d="M16.5 14.4c2.4.1 4 1.8 4 4.6" /></>),
  web: (s = 20) => svg(s, <><rect x="3.5" y="4.5" width="17" height="15" rx="2" /><line x1="3.5" y1="9" x2="20.5" y2="9" /><circle cx="6.5" cy="6.8" r=".6" fill="currentColor" /><circle cx="9" cy="6.8" r=".6" fill="currentColor" /></>),
  settings: (s = 20) => svg(s, <><line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="17" x2="20" y2="17" /><circle cx="9" cy="7" r="2.4" fill="var(--card)" /><circle cx="15" cy="17" r="2.4" fill="var(--card)" /></>),
}
