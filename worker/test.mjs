/**
 * Prüft den Worker OHNE Deploy: echte Ed25519-Schlüssel, echte Signaturen,
 * echte Requests gegen worker.fetch().
 *
 *   node test.mjs
 *
 * Wichtigster Fall ist der zweite: Discord testet die Endpoint-URL beim
 * Eintragen absichtlich mit kaputter Signatur und akzeptiert sie nur, wenn wir
 * mit 401 ablehnen. Läuft der Test durch, wird Discord die URL annehmen.
 *
 * Braucht Node >= 18 (WebCrypto mit Ed25519).
 */
import worker from "./src/index.js";

const enc = new TextEncoder();
const hex = (buf) =>
  [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const env = { DISCORD_PUBLIC_KEY: hex(await crypto.subtle.exportKey("raw", pair.publicKey)) };

async function post(bodyObj, { tamper = false } = {}) {
  const body = JSON.stringify(bodyObj);
  const ts = String(Math.floor(Date.now() / 1000));
  const sig = hex(
    await crypto.subtle.sign({ name: "Ed25519" }, pair.privateKey, enc.encode(ts + body))
  );
  const req = new Request("https://x/", {
    method: "POST",
    headers: {
      "x-signature-ed25519": tamper ? "00".repeat(64) : sig,
      "x-signature-timestamp": ts,
    },
    body,
  });
  const res = await worker.fetch(req, env);
  return { status: res.status, text: await res.text() };
}

let failed = 0;
function check(label, cond, detail = "") {
  console.log(`${cond ? "  ok  " : " FAIL "} ${label}${detail ? "  → " + detail : ""}`);
  if (!cond) failed++;
}

let r = await post({ type: 1 });
check("PING → PONG", r.status === 200 && JSON.parse(r.text).type === 1, r.text);

r = await post({ type: 1 }, { tamper: true });
check("kaputte Signatur → 401", r.status === 401, `${r.status} ${r.text}`);

r = await worker.fetch(new Request("https://x/", { method: "POST", body: "{}" }), env);
check("fehlende Header → 401", r.status === 401);

r = await post({ type: 2, data: { name: "ping" } });
let body = JSON.parse(r.text);
check("/ping → pong", r.status === 200 && body.data.content.startsWith("pong"), body.data?.content);
check("/ping nur für Aufrufer sichtbar", body.data.flags === 64, `flags=${body.data.flags}`);

r = await post({ type: 2, data: { name: "gibtsnicht" } });
check("unbekannter Command → Hinweis", JSON.parse(r.text).data.content.includes("gibtsnicht"));

r = await worker.fetch(new Request("https://x/", { method: "GET" }), env);
check("GET → 200", r.status === 200);

console.log(failed ? `\n${failed} FEHLER` : "\nALLES GRÜN");
process.exit(failed ? 1 : 0);
