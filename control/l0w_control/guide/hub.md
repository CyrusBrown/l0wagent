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
- **Self-check** (daily, or **Check now**): the hub's directory serves your current key, and both
  hostnames answer from outside. A failed check sends a notification and shows a card on Home.
- **Hub key alarm**: the hub's own key changed without a rotation from the one you joined with.
  Check with the operator before trusting it.
- **Rotate key**: replaces the signing key; the old key signs the change, so friends can follow it.
- **Recover with recovery key**: when the signing key may be stolen, replace it with a change
  signed by the recovery key.
- **Leave hub**: type your address to confirm. The hostnames stop working and the name is gone for good.
- Backups (`./l0w backup`) include the instance's keys.
