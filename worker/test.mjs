/**
 * Prüft den Worker OHNE Deploy: echte Ed25519-Schlüssel, echte Signaturen,
 * echte Requests gegen worker.fetch().
 *
 *   node test.mjs
 *
 * Discord und GitHub werden über ein ersetztes `fetch` nachgestellt. Der
 * Worker-Code bleibt dadurch frei von Test-Sonderwegen — er ruft ganz normal
 * seine URLs auf, nur antwortet hier eben die Attrappe.
 *
 * Zwei Fälle sind wichtiger als der Rest:
 *   - "kaputte Signatur → 401": Discord testet die Endpoint-URL beim Eintragen
 *     genau damit und akzeptiert sie nur, wenn abgelehnt wird.
 *   - "order-state.json wird nie geschrieben": Der Actions-Bot arbeitet auf
 *     dieser Datei nach dem Muster laden→ändern→speichern. Schriebe der Worker
 *     mit hinein, gingen Änderungen still verloren.
 *
 * Braucht Node >= 18 (WebCrypto mit Ed25519).
 */
import worker from "./src/index.js";

const enc = new TextEncoder();
const hex = (buf) =>
  [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const PUBLIC_KEY = hex(await crypto.subtle.exportKey("raw", pair.publicKey));

const FULL_ENV = {
  DISCORD_PUBLIC_KEY: PUBLIC_KEY,
  DISCORD_BOT_TOKEN: "bot-token",
  GIST_TOKEN: "gist-token",
  GIST_ID: "gist-id",
};

const GUILD = "guild-1";
const OWNER = "user-owner";
const OTHER = "user-other";
const ROLE = "role-bgnotify";

// --------------------------------------------------------------------------
// Attrappe für Discord + GitHub
// --------------------------------------------------------------------------
let fake;

function resetFake(overrides = {}) {
  fake = {
    commands: { guilds: {} }, // Inhalt von commands.json
    roles: [], // Rollen der Gilde
    ownerId: OWNER,
    assigned: [], // [{ userId, roleId }]
    followUp: "", // zuletzt nachgereichte Antwort
    patchedFiles: [], // welche Gist-Dateien angefasst wurden
    ...overrides,
  };
}

const jsonRes = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

globalThis.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === "string" ? input : input.url);
  const method = (init.method || "GET").toUpperCase();
  const path = url.pathname;

  // ---- GitHub Gist ----
  if (url.host === "api.github.com") {
    if (method === "GET") {
      return jsonRes({
        files: {
          "commands.json": { content: JSON.stringify(fake.commands), truncated: false },
          // Muss unberührt bleiben — der Actions-Bot arbeitet darauf.
          "order-state.json": { content: '{"accounts":{}}', truncated: false },
        },
      });
    }
    if (method === "PATCH") {
      const body = JSON.parse(init.body);
      fake.patchedFiles = Object.keys(body.files || {});
      if (body.files?.["commands.json"]) {
        fake.commands = JSON.parse(body.files["commands.json"].content);
      }
      return jsonRes({ ok: true });
    }
  }

  // ---- Discord ----
  if (url.host === "discord.com") {
    if (method === "PATCH" && path.includes("/messages/@original")) {
      fake.followUp = JSON.parse(init.body).content;
      return jsonRes({ ok: true });
    }
    if (method === "GET" && path === `/api/v10/guilds/${GUILD}`) {
      return jsonRes({ id: GUILD, owner_id: fake.ownerId });
    }
    if (method === "GET" && path === `/api/v10/guilds/${GUILD}/roles`) {
      return jsonRes(fake.roles);
    }
    if (method === "POST" && path === `/api/v10/guilds/${GUILD}/roles`) {
      const role = { id: ROLE, name: JSON.parse(init.body).name };
      fake.roles.push(role);
      return jsonRes(role);
    }
    // /api/v10/guilds/{guild}/members/{user}/roles/{role}
    if (method === "PUT" && path.includes("/members/")) {
      const [userId, , roleId] = path.split("/members/")[1].split("/");
      fake.assigned.push({ userId, roleId });
      return new Response(null, { status: 204 });
    }
  }

  throw new Error(`unerwarteter Aufruf: ${method} ${url.href}`);
};

// --------------------------------------------------------------------------
// Hilfsmittel
// --------------------------------------------------------------------------
async function post(bodyObj, { tamper = false, env = FULL_ENV } = {}) {
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
  const res = await worker.fetch(req, env); // absichtlich ohne ctx → /setup laeuft synchron
  const text = await res.text();
  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    /* kein JSON, z.B. bei 401 */
  }
  return { status: res.status, text, body: parsed, content: parsed?.data?.content };
}

const cmd = (name, { roles = [], userId = OTHER, guild = GUILD, dm = false } = {}) => ({
  type: 2,
  application_id: "app-1",
  token: "interaction-token",
  ...(dm ? { user: { id: userId } } : { guild_id: guild, member: { user: { id: userId }, roles } }),
  data: { name },
});

let failed = 0;
function check(label, cond, detail = "") {
  console.log(`${cond ? "  ok  " : " FAIL "} ${label}${detail ? "  → " + detail : ""}`);
  if (!cond) failed++;
}
const section = (t) => console.log(`\n── ${t} ──`);

// --------------------------------------------------------------------------
section("Signatur & Transport");
// --------------------------------------------------------------------------
resetFake();

let r = await post({ type: 1 });
check("PING → PONG", r.status === 200 && r.body.type === 1, r.text);

r = await post({ type: 1 }, { tamper: true });
check("kaputte Signatur → 401", r.status === 401, `${r.status} ${r.text}`);

r = await worker.fetch(new Request("https://x/", { method: "POST", body: "{}" }), FULL_ENV);
check("fehlende Header → 401", r.status === 401);

r = await worker.fetch(new Request("https://x/", { method: "GET" }), FULL_ENV);
check("GET → 200", r.status === 200);

// --------------------------------------------------------------------------
section("Rollenprüfung");
// --------------------------------------------------------------------------
resetFake();

r = await post(cmd("ping"));
check("/ping vor der Einrichtung → Hinweis auf /setup", r.content?.includes("/setup"), r.content);

// Ab hier ist der Server eingerichtet.
resetFake({ commands: { guilds: { [GUILD]: { role_id: ROLE } } }, roles: [{ id: ROLE, name: "bgnotify" }] });

r = await post(cmd("ping", { roles: [] }));
check("/ping OHNE Rolle → abgelehnt", r.content?.includes("Rolle") && !r.content?.includes("pong"), r.content);

r = await post(cmd("ping", { roles: [ROLE] }));
check("/ping MIT Rolle → pong", r.content?.startsWith("pong"), r.content);
check("/ping nur für Aufrufer sichtbar", r.body.data.flags === 64, `flags=${r.body.data.flags}`);

r = await post(cmd("ping", { roles: ["irgendeine-andere"] }));
check("/ping mit fremder Rolle → abgelehnt", !r.content?.includes("pong"), r.content);

r = await post(cmd("ping", { dm: true }));
check("/ping per Direktnachricht → abgelehnt", r.content?.includes("Direktnachrichten"), r.content);

r = await post(cmd("gibtsnicht", { roles: [ROLE] }));
check("unbekannter Command → Hinweis", r.content?.includes("gibtsnicht"), r.content);

// --------------------------------------------------------------------------
section("/setup");
// --------------------------------------------------------------------------
resetFake();

r = await post(cmd("setup", { userId: OTHER }));
check("/setup als Nicht-Inhaber → abgelehnt", fake.followUp.includes("Server-Inhaber"), fake.followUp);
check("/setup legt dabei keine Rolle an", fake.roles.length === 0);

resetFake();
r = await post(cmd("setup", { userId: OWNER }));
check("/setup antwortet deferred (Typ 5)", r.body.type === 5, `type=${r.body.type}`);
check("/setup legt die Rolle an", fake.roles.some((x) => x.name === "bgnotify"));
check("/setup gibt sie dem Aufrufer", fake.assigned.some((a) => a.userId === OWNER && a.roleId === ROLE));
check("/setup merkt sie im Gist", fake.commands.guilds[GUILD]?.role_id === ROLE);
check(
  "/setup schreibt NUR commands.json",
  fake.patchedFiles.length === 1 && fake.patchedFiles[0] === "commands.json",
  fake.patchedFiles.join(", ")
);

// Rolle existiert schon, Gist-Eintrag fehlt (z.B. Stand verloren) → nicht doppelt anlegen.
resetFake({ roles: [{ id: ROLE, name: "bgnotify" }] });
r = await post(cmd("setup", { userId: OWNER }));
check("/setup übernimmt vorhandene Rolle statt Duplikat", fake.roles.length === 1, `${fake.roles.length} Rollen`);
check("… und meldet das auch so", fake.followUp.includes("bestehende Rolle"), fake.followUp);

// Nach /setup kann der Inhaber die Commands sofort benutzen.
r = await post(cmd("ping", { userId: OWNER, roles: [ROLE] }));
check("nach /setup: /ping funktioniert", r.content?.startsWith("pong"), r.content);

// --------------------------------------------------------------------------
section("Fehlende Secrets");
// --------------------------------------------------------------------------
resetFake();
r = await post(cmd("setup", { userId: OWNER }), { env: { DISCORD_PUBLIC_KEY: PUBLIC_KEY } });
check("/setup ohne Secrets → nennt die fehlenden", r.content?.includes("DISCORD_BOT_TOKEN"), r.content);

r = await post(cmd("ping"), { env: { DISCORD_PUBLIC_KEY: PUBLIC_KEY } });
check("/ping ohne Gist-Zugang → sagt das klar", r.content?.includes("GIST_TOKEN"), r.content);

console.log(failed ? `\n${failed} FEHLER` : "\nALLES GRÜN");
process.exit(failed ? 1 : 0);
