#!/usr/bin/env node
// Print a fresh invite line from a running hub (calls POST /v0/admin/invites).
//
//   HUB_ADMIN_TOKEN=... node scripts/invite.mjs --hub https://hub.example [--name-hint mara] [--days 7]
//                                               [--reregister NAME] [--connect 127.0.0.1:8787]
//
// --connect HOST:PORT opens the connection to that address while keeping the Host header (for a hub
// whose hostname does not resolve from here, such as hub.localhost in the conformance suite).
// The last line printed is the `l0w-invite:...` string, which is what the suite's --invite-cmd reads.
import http from "node:http";
import https from "node:https";

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) throw new Error(`unexpected argument ${a}`);
    const [k, inline] = a.slice(2).split("=", 2);
    out[k] = inline ?? argv[++i];
    if (out[k] === undefined) throw new Error(`--${k} needs a value`);
  }
  return out;
}

function post(url, headers, body, connect) {
  const u = new URL(url);
  const lib = u.protocol === "https:" ? https : http;
  let host = u.hostname;
  let port = u.port || (u.protocol === "https:" ? 443 : 80);
  if (connect) {
    const i = connect.lastIndexOf(":");
    host = connect.slice(0, i);
    port = Number(connect.slice(i + 1));
  }
  return new Promise((resolve, reject) => {
    const req = lib.request(
      { host, port, method: "POST", path: u.pathname, servername: u.hostname, headers: { host: u.host, ...headers, "content-length": Buffer.byteLength(body) } },
      (res) => {
        let data = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode, body: data }));
      },
    );
    req.on("error", reject);
    req.setTimeout(30_000, () => req.destroy(new Error("timed out")));
    req.end(body);
  });
}

async function main() {
  const a = parseArgs(process.argv.slice(2));
  const token = process.env.HUB_ADMIN_TOKEN;
  if (!a.hub || !token) {
    console.error("usage: HUB_ADMIN_TOKEN=... invite.mjs --hub URL [--name-hint NAME] [--days N] [--reregister NAME] [--connect HOST:PORT]");
    return 2;
  }
  const payload = {};
  if (a["name-hint"]) payload.name_hint = a["name-hint"];
  if (a.days) payload.days = Number(a.days);
  if (a.reregister) payload.reregister = a.reregister;
  const r = await post(
    a.hub.replace(/\/+$/, "") + "/v0/admin/invites",
    { authorization: `Bearer ${token}`, "content-type": "application/json" },
    JSON.stringify(payload),
    a.connect,
  );
  let body;
  try {
    body = JSON.parse(r.body);
  } catch {
    body = {};
  }
  if (r.status < 200 || r.status > 299 || typeof body.invite !== "string") {
    console.error(`the hub answered ${r.status}: ${r.body.slice(0, 200)}`);
    return 1;
  }
  console.log(body.invite);
  return 0;
}

main().then(
  (code) => process.exit(code),
  (e) => {
    console.error(String(e?.message ?? e));
    process.exit(1);
  },
);
