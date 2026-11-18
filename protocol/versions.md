# Versions

- The protocol version is a string `l0w/<major>.<minor>`. This draft is `l0w/0.1`. While the major is 0,
  any minor change may break compatibility; from `l0w/1.0`, a minor change only adds.
- Every document and every hub response carries `"v"`. Receivers MUST ignore fields they don't know,
  and MUST refuse a document whose `v` they don't speak (with `400 {"error": "unsupported_version", "supported": [...]}`).
- Hubs list the versions they speak in their capabilities (`protocols`), instances in their `self`
  document. Two parties use the highest version both list.
- **Instance releases**: a hub supports the current and the previous instance release
  (`instance_releases.min` in capabilities). An instance older than that is told so at the tunnel
  handshake (close code **4426**, "upgrade required") and in the join response (`426 instance_too_old`),
  and keeps working on its LAN.
- Hub URLs carry a path version (`/v0/`) independent of the protocol version; it changes only when
  the URL layout does.
