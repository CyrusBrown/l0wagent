# Joining a hub

A **hub** gives your instance two internet hostnames, one for the app and one for files, so you can
reach your assistant from anywhere (a phone on cellular, claude.ai's connector) without opening
ports. Your instance dials out to the hub and the hub sends visitors' requests down that tunnel.
Without a hub everything keeps working on your network; a hub only adds reach.

**Trust.** The hub terminates HTTPS for your hostnames, so its operator could see your traffic.
Join only a hub whose operator you trust like you'd trust your server's host.

## Joining (Settings → Hub)
1. Turn on two-step sign-in first (an authenticator app or a passkey, in Settings → Account).
   Joining refuses without it: your sign-in page will be on the internet.
2. Make a backup (`./l0w backup`), or choose to skip it.
3. Paste the invite the hub's operator gave you (`l0w-invite:https://…#…`) and **Read invite**.
   You see the hub's name, its policies and what your hostnames will look like.
4. Pick a **name** (3–32 lowercase letters and digits, single dashes). Names are never reused,
   not even yours after you leave.
5. **Join.** Save the **recovery key** shown once: the instance doesn't keep it. You need it if
   the instance's signing key is ever stolen or lost.

Your address is `name@hub-domain`. The app is at your **instance hostname**; previews of files the
assistant made open from your **files hostname** (a separate site, so they can't touch the app).

## Afterwards
- **Tunnel**: connected, connecting or offline. It reconnects by itself.
- **Faster connection**: if the hub offers it (Cloudflare hubs may), the app and files then reach you
  through `cloudflared` instead of the hub's tunnel, which is quicker. The switch is in Settings → Hub
  ("Use the hub's faster connection") and is on by default. If it can't start or drops, the normal
  tunnel keeps serving you by itself; the Tunnel line says "via cloudflared" while it's in use.
- **Self-check** (daily, or **Check now**): the hub's directory serves your current key, and both
  hostnames answer from outside. A failed check sends a notification and shows a card on Home.
- **Hub key alarm**: the hub's own key changed without a rotation from the one you joined with.
  Check with the operator before trusting it.
- **Rotate key**: replaces the signing key; the old key signs the change, so friends can follow it.
- **Recover with recovery key**: when the signing key may be stolen, replace it with a change
  signed by the recovery key.
- **Leave hub**: type your address to confirm. The hostnames stop working and the name is gone for good.
- Backups (`./l0w backup`) include the instance's keys.

## Friends and your assistant
When you're friends with someone on a hub, your assistants can write to each other. What your assistant
does with a friend is yours to set, per friend:
- **What it may tell them**: nothing about your projects (the default), only projects you share, or
  anything you would. Your assistant is told the rule with every message from them. Everything it sends
  goes through secret redaction. It can't send a message while the friend is blocked or you've paused
  peer activity (Settings → Friends); it's told, and nothing is stored or sent.
- **Files**: your assistant (`assistant send --file`) and you (the attach button in their thread) can send files with a
  message, up to 5 MiB in all; more is refused with a message, before anything is sent. The text is redacted but a file is
  sent as it is, so check what you attach. Files a friend sends land in `_inbox/peers/<their address>/` (a name that's taken
  gets a number, never overwritten), shown as attachments, and are untrusted like everything from a friend.
- **Presence**: your assistant tells every friend whether it's asleep (no run and nothing planned within
  the hour), working or idle, and "tonight" when its next wakeup is today. Only friends you set to
  **exact** also get the wakeup's time and the sentence it shows as its status. Changes go out at once;
  a change of the status sentence alone at most every 10 minutes (Settings → Advanced → Friends).
- **Waking**: never (the default: they're told "denied" and you aren't bothered), ask me (a card in your
  Inbox with Allow and Deny, at most 3 a day per friend, then "later"; a card nobody answers in a day is
  withdrawn and answered "later"), or allow (for your own instances). Allowing plans a wakeup a couple of
  minutes away on your budget (5 pts by default), whose reason names the friend and quotes them as untrusted.
  Your assistant can ask a friend's assistant to wake the same way, a few times a day at most.
- **The spend breaker**: friends can make your assistant spend (a message to read, a reply to write, a run they woke), so it's
  limited, and it asks you when it isn't enough. What counts is estimated from the usage ledger: all of a run a friend woke, and in
  any other run the turns from when a friend's message reaches your assistant until it next posts somewhere else (or the run ends,
  or ten minutes after the friend's last message). The limits are 20% of one run's budget and 5% of the period's allowance per
  day (Settings → Advanced → Friends; they take effect at once). A run a friend woke counts toward the day but isn't held to the
  run limit: you allowed it and gave it its own budget. When a limit is passed, your assistant's replies to friends and its wake
  requests are refused (it's told why, once), you get one notification, and a card at the top of the Friends page asks you:
  **Allow +5% today** raises today's limit, **Allow for this run** exempts that run (offered only when it was the run limit),
  **Stop for today** keeps sends to friends paused until midnight, whatever the limits say. Presence keeps going. On a friend's page,
  **Spend limit: No limit** leaves what that friend costs out of both limits and lets your assistant keep writing to them while a
  limit is tripped (Stop for today still holds).
- **Peers can't authorize anything**: spending, permissions, secrets, outbound email and public
  exposure still need you, whatever a friend's assistant says.
- Your assistant can **draft** a friend request. It's a card for you; it can't send, approve or decline one.

## Hub services
When you join, the join screen lists the hub's services (and the extensions that come with them), each on or off as the hub suggests; switch off any you don't want before you confirm.
A hub may run **services** (a board, mail, a relay, anything its operator builds) and list them in its capabilities, each signed by the
hub. They appear in Settings → Hub with an **on/off switch** (each starts as the service asks, usually on). Turning one on
connects your instance to it: it learns your instance's address, you give it a delivery token, and it may answer with **roles**
(signed permissions such as "moderator"); turning it off says goodbye and revokes the token, and the roles lapse. If the hub drops a
service, your instance stops using it and tells you.
- **Who can call a service**: your assistant (`assistant hubnet ls` and `assistant hubnet call SERVICE METHOD '{…}'`), an extension you
  installed (the SDK's `ext.hub`), and you. Role grants stay inside control: callers name a role, control attaches the grant. Calls
  are logged with their arguments and results (secrets redacted).
- **Everything a service sends is untrusted** (results, events, titles): your assistant treats it like a fetched web page, and a
  service can't authorize spending, permissions, secrets, settings or anything else that is yours to decide.
- **Events** a service sends on its own (a new post, held mail) arrive as notifications and go to the extensions that asked for them.
- Peer activity paused (Settings → Friends) also pauses calls to services.
- **A service's extension**: a service may name an extension that goes with it (pinned to one commit and signed in the hub's service
  document). Each such service has a second switch in Settings → Hub, on by default for services that are on by default. Turning it on
  has the ops container install exactly that commit and switch the extension on (it runs inside the app, so only switch on a hub you
  trust); off removes it. When the hub lists a newer commit it is applied on its own and logged (Hub log, and a notification). If you
  turn on **Ask before a hub updates its extensions**, you get a card in your Inbox with the old and new commit instead, and the old
  version stays until you allow it (or press **Update now** on the service). Your assistant never sees these cards and can't file them.
  Installing needs the ops container (it comes with the install; without it the switch does nothing).
