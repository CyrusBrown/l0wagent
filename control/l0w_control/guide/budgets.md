# Budgets, levels and limits

Budgets are in **points**. On a Claude subscription 1 pt is 0.1% of the week (1,000 pts a week);
on an Anthropic API key or opencode Go, 1 pt is $0.10.

- **Levels** (Settings → Budgets & levels): Dormant (Reply now only), Frugal (50 pts a week),
  Steady (150), Busy (400), Max (850). Each level also sets allowed hours, wakeups per day, the
  gap between them, the most points per wakeup, which models the assistant may use, and whether
  the fallback and sweep are on.
- **Customize {level}**: change any of the level's limits; each changed field shows the level's
  own value and a Reset, and **Back to {level}'s settings** clears all changes. Picking a level
  again also clears them.
- **Always, whatever the level**: the **reply budget** (for Reply now, separate from the
  allowance), the **reserve** (Claude subscription: no wakeup starts above 80% of the week or 70%
  of the 5-hour window, protecting your own use), and the **budget period** (other providers).
- **Budget period**: a week, or a month from a reset day. opencode Go is monthly by default, with
  **caps**: at most 20% of the month in any 5 hours and 50% in any 7 days, like Go's own limits.
  Level presets are per week and scale to a month; an allowance you set is per period.
- **Boost**: use the next level's limits until the reset or for 24 hours.
- **Sweep** (Claude subscription, Busy and Max): near the weekly reset, extra wakeups spend what
  you didn't use, up to a ceiling.
- **Usage**: Settings and Home show your own use, your 1:1 chats, and the assistant's. 1:1 chats
  are your own use and don't spend the assistant's allowance (an optional weekly cap per provider
  stops them).
