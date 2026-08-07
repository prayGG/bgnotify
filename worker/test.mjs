/**
 * Prüft den Worker OHNE Deploy: echte Ed25519-Schlüssel, echte Signaturen,
 * echte Requests gegen worker.fetch().
 *
 *   node test.mjs
 *
 * Discord, die Gist-API und raw.githubusercontent werden über ein ersetztes
 * `fetch` nachgestellt. Der Worker-Code bleibt dadurch frei von
 * Test-Sonderwegen — er ruft ganz normal seine URLs auf, nur antwortet hier
 * eben die Attrappe.
 *
 * Drei Fälle tragen mehr als der Rest:
 *   - "kaputte Signatur → 401": Discord testet die Endpoint-URL beim Eintragen
 *     genau damit und akzeptiert sie nur, wenn abgelehnt wird.
 *   - "order-state.json wird nie geschrieben": Der Actions-Bot arbeitet auf
 *     dieser Datei nach dem Muster laden→ändern→speichern. Schriebe der Worker
 *     mit hinein, gingen Änderungen still verloren.
 *   - "Panel wird bearbeitet, nicht neu gepostet": sonst füllt sich der
 *     Channel bei jeder Katalogänderung mit Leichen.
 *
 * Braucht Node >= 18 (WebCrypto mit Ed25519).
 */
import worker from "./src/index.js";
import { COMMANDS, catalogVersion, flatten, toDiscordPayload } from "./src/catalog.js";

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
const CHANNEL = "channel-1";
const OWNER = "user-owner";
const OTHER = "user-other";
const ROLE = "role-bgnotify";

/** Eingerichteter Server. Als Funktion, damit jeder Test frische Objekte
 *  bekommt — ein geteiltes Literal würde Mutationen weiterreichen. */
const ready = () => ({
  commands: { guilds: { [GUILD]: { role_id: ROLE } } },
  roles: [{ id: ROLE, name: "bgnotify" }],
});

// --------------------------------------------------------------------------
// Attrappe
// --------------------------------------------------------------------------
let fake;

function resetFake(overrides = {}) {
  fake = {
    commands: { guilds: {} },
    orders: {},
    repoState: {},
    configYml: "",
    roles: [],
    ownerId: OWNER,
    assigned: [],
    followUp: "",
    patchedFiles: [],
    posted: [],
    editedMessages: [],
    ...overrides,
  };
}

const jsonRes = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

globalThis.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === "string" ? input : input.url);
  const method = (init.method || "GET").toUpperCase();
  const path = url.pathname;

  if (url.host === "raw.githubusercontent.com") {
    if (path.endsWith("/state.json")) return jsonRes(fake.repoState);
    if (path.endsWith("/config.yml")) return new Response(fake.configYml);
    return new Response("", { status: 404 });
  }

  if (url.host === "api.github.com") {
    if (method === "GET") {
      return jsonRes({
        files: {
          "commands.json": { content: JSON.stringify(fake.commands) },
          "order-state.json": { content: JSON.stringify(fake.orders) },
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

  if (url.host === "discord.com") {
    // Muss VOR der Channel-Regel stehen: die nachgereichte Antwort läuft auch
    // über einen PATCH auf .../messages/...
    if (method === "PATCH" && path.includes("/messages/@original")) {
      fake.followUp = JSON.parse(init.body).content;
      return jsonRes({ ok: true });
    }
    if (method === "POST" && /\/channels\/[^/]+\/messages$/.test(path)) {
      const body = JSON.parse(init.body);
      fake.posted.push({ channelId: path.split("/")[4], embed: body.embeds[0] });
      return jsonRes({ id: `msg-${fake.posted.length}` });
    }
    if (method === "PATCH" && /\/channels\/[^/]+\/messages\/[^/]+$/.test(path)) {
      const messageId = path.split("/").pop();
      if (fake.missingMessage === messageId) return new Response("", { status: 404 });
      fake.editedMessages.push({ messageId, embed: JSON.parse(init.body).embeds[0] });
      return jsonRes({ id: messageId });
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
  const res = await worker.fetch(
    new Request("https://x/", {
      method: "POST",
      headers: {
        "x-signature-ed25519": tamper ? "00".repeat(64) : sig,
        "x-signature-timestamp": ts,
      },
      body,
    }),
    env // absichtlich ohne ctx → deferrte Abläufe laufen synchron
  );
  const text = await res.text();
  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    /* kein JSON, z.B. bei 401 */
  }
  return {
    status: res.status,
    text,
    body: parsed,
    content: parsed?.data?.content,
    embed: parsed?.data?.embeds?.[0],
  };
}

const cmd = (path, { roles = [ROLE], userId = OTHER, dm = false } = {}) => {
  const [name, sub] = path.split(" ");
  return {
    type: 2,
    application_id: "app-1",
    token: "interaction-token",
    channel_id: CHANNEL,
    ...(dm ? { user: { id: userId } } : { guild_id: GUILD, member: { user: { id: userId }, roles } }),
    data: { name, ...(sub ? { options: [{ type: 1, name: sub }] } : {}) },
  };
};

let failed = 0;
function check(label, cond, detail = "") {
  console.log(`${cond ? "  ok  " : " FAIL "} ${label}${detail ? "  → " + detail : ""}`);
  if (!cond) failed++;
}
const section = (t) => console.log(`\n── ${t} ──`);
const textOf = (embed) =>
  [embed?.description || "", ...(embed?.fields || []).map((f) => `${f.name} ${f.value}`)].join("\n");

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
r = await post(cmd("ping", { roles: [] }));
check("vor der Einrichtung → Hinweis auf /setup", r.content?.includes("/setup"), r.content);

resetFake(ready());
r = await post(cmd("ping", { roles: [] }));
check("/ping OHNE Rolle → abgelehnt", !r.content?.includes("pong"), r.content);

r = await post(cmd("ping"));
check("/ping MIT Rolle → pong", r.content?.startsWith("pong"), r.content);
check("nur für Aufrufer sichtbar", r.body.data.flags === 64, `flags=${r.body.data.flags}`);

r = await post(cmd("status", { roles: [] }));
check("auch /status ist gesperrt", !r.embed, r.content);

r = await post(cmd("ping", { dm: true }));
check("Direktnachricht → abgelehnt", r.content?.includes("Direktnachrichten"), r.content);

r = await post(cmd("gibtsnicht"));
check("unbekannter Command → Hinweis", r.content?.includes("gibtsnicht"), r.content);

// --------------------------------------------------------------------------
section("/setup");
// --------------------------------------------------------------------------
resetFake();
r = await post(cmd("setup", { userId: OTHER }));
check("als Nicht-Inhaber → abgelehnt", fake.followUp.includes("Server-Inhaber"), fake.followUp);
check("… und legt keine Rolle an", fake.roles.length === 0);

resetFake();
r = await post(cmd("setup", { userId: OWNER }));
check("antwortet deferred (Typ 5)", r.body.type === 5, `type=${r.body.type}`);
check("legt die Rolle an", fake.roles.some((x) => x.name === "bgnotify"));
check("gibt sie dem Aufrufer", fake.assigned.some((a) => a.userId === OWNER && a.roleId === ROLE));
check("merkt sie im Gist", fake.commands.guilds[GUILD]?.role_id === ROLE);
check(
  "schreibt NUR commands.json",
  fake.patchedFiles.join(",") === "commands.json",
  fake.patchedFiles.join(", ")
);

resetFake({ roles: [{ id: ROLE, name: "bgnotify" }] });
r = await post(cmd("setup", { userId: OWNER }));
check("übernimmt vorhandene Rolle statt Duplikat", fake.roles.length === 1, `${fake.roles.length} Rollen`);

// --------------------------------------------------------------------------
section("/status");
// --------------------------------------------------------------------------
const frisch = new Date(Date.now() - 5 * 60000).toISOString();
resetFake({
  ...ready(),
  repoState: { bot_stats: { last_check_at: frisch, total_checks: 10804, total_restocks: 3 } },
  orders: {
    enabled: { a: "on", b: "off" },
    accounts: { a: { last_check_at: frisch, orders: { 1: { status: "processing" } } }, b: {} },
    auto_tracking: { "pray #37143": { url: "https://t/1" } },
    manual_tracking_state: { "pray #37143": { status: "unterwegs", last_check_at: frisch } },
  },
});
r = await post(cmd("status"));
let t = textOf(r.embed);
check("meldet den Bot als laufend", t.includes("läuft"), t.split("\n").find((l) => l.includes("läuft")));
check("zählt aktive Konten", t.includes("2 hinterlegt") && t.includes("**1** aktiv"));
check("zählt Sendungen unterwegs", t.includes("**1** unterwegs"));
check("zeigt die Gesamtzahl der Läufe", t.includes("10.804"));

resetFake({
  ...ready(),
  repoState: { bot_stats: { last_check_at: new Date(Date.now() - 6 * 3600e3).toISOString() } },
});
r = await post(cmd("status"));
check("erkennt einen ausgefallenen Bot", textOf(r.embed).includes("kein Lauf"), textOf(r.embed));

resetFake({ ...ready(), repoState: { error_report: { active: true } } });
r = await post(cmd("status"));
check("meldet Fehler des letzten Laufs", textOf(r.embed).includes("Fehler"));

// --------------------------------------------------------------------------
section("/track list");
// --------------------------------------------------------------------------
resetFake(ready());
r = await post(cmd("track list"));
check("ohne Sendungen → freundlicher Hinweis", r.embed.description.includes("Nichts in Verfolgung"));

resetFake({
  ...ready(),
  orders: {
    auto_tracking: { "pray #37143": { url: "https://t/1" } },
    manual_tracking: { mave: { url: "https://t/2" } },
    manual_tracking_state: {
      "pray #37143": { status: "Sendung ist unterwegs", last_check_at: frisch },
      mave: { status: "Sendung wurde zugestellt.", last_check_at: frisch },
    },
  },
});
r = await post(cmd("track list"));
t = r.embed.description;
check("listet beide Quellen", t.includes("pray #37143") && t.includes("mave"));
check("kennzeichnet Herkunft", t.includes("automatisch") && t.includes("von Hand"));
check("erkennt Zustellung", t.includes("✅"), t.split("\n")[2]);
check("unterwegs steht oben", t.indexOf("🚚") < t.indexOf("✅"));

// --------------------------------------------------------------------------
section("/account list");
// --------------------------------------------------------------------------
resetFake({
  ...ready(),
  configYml: "accounts:\n    - name: a\n      label: pray\n    - name: b\n      label: mave\n",
  orders: {
    enabled: { a: "on", b: "off" },
    accounts: {
      a: { last_check_at: frisch, orders: { 1: { status: "processing" }, 2: { status: "completed" } } },
      b: { orders: {} },
    },
  },
});
r = await post(cmd("account list"));
t = r.embed.description;
check("nutzt die Namen aus config.yml", t.includes("pray") && t.includes("mave"), t.split("\n")[0]);
check("zeigt an/aus", t.includes("🟢") && t.includes("⚪"));
check("zählt offene Bestellungen", t.includes("**1** offen"));
check("nennt KEINE Bestellnummern", !t.includes("#1") && !t.includes("#2"));

// --------------------------------------------------------------------------
section("/panel");
// --------------------------------------------------------------------------
resetFake(ready());
r = await post(cmd("panel", { userId: OTHER }));
check("als Nicht-Inhaber → abgelehnt", fake.followUp.includes("Server-Inhaber"), fake.followUp);
check("… und postet nichts", fake.posted.length === 0);

resetFake(ready());
r = await post(cmd("panel", { userId: OWNER }));
check("postet die Übersicht", fake.posted.length === 1, `${fake.posted.length} Nachricht(en)`);
check("in den aufrufenden Channel", fake.posted[0]?.channelId === CHANNEL);
check("merkt sich die Nachricht", fake.commands.guilds[GUILD].panel?.message_id === "msg-1");
check("merkt sich den Katalogstand", fake.commands.guilds[GUILD].panel?.version === catalogVersion());

const alleBefehle = flatten().every((c) => textOf(fake.posted[0].embed).includes(`/${c.path}`));
check("nennt jeden Command aus dem Katalog", alleBefehle);

r = await post(cmd("panel", { userId: OWNER }));
check("zweiter Aufruf bearbeitet statt neu zu posten", fake.posted.length === 1 && fake.editedMessages.length === 1);

// Panel wurde im Channel gelöscht → neu anlegen statt Fehler.
resetFake({ ...ready(), missingMessage: "msg-weg" });
fake.commands.guilds[GUILD].panel = { channel_id: CHANNEL, message_id: "msg-weg", version: "alt" };
r = await post(cmd("panel", { userId: OWNER }));
check("gelöschtes Panel wird neu gepostet", fake.posted.length === 1, fake.followUp);

// Veralteter Katalog → beim nächsten beliebigen Command nachziehen.
resetFake(ready());
fake.commands.guilds[GUILD].panel = { channel_id: CHANNEL, message_id: "msg-1", version: "veraltet" };
r = await post(cmd("ping"));
check("veraltetes Panel zieht sich selbst nach", fake.editedMessages.length === 1);
check("… und merkt sich den neuen Stand", fake.commands.guilds[GUILD].panel.version === catalogVersion());

resetFake(ready());
fake.commands.guilds[GUILD].panel = { channel_id: CHANNEL, message_id: "msg-1", version: catalogVersion() };
r = await post(cmd("ping"));
check("aktuelles Panel wird NICHT angefasst", fake.editedMessages.length === 0);

// --------------------------------------------------------------------------
section("Katalog");
// --------------------------------------------------------------------------
const payload = toDiscordPayload();
check("Registrierung enthält alle Top-Level-Commands", payload.length === COMMANDS.length);
check(
  "Untercommands werden zu Options vom Typ 1",
  payload.find((c) => c.name === "track").options[0].type === 1
);
check(
  "alle Beschreibungen ≤ 100 Zeichen (Discord-Grenze)",
  payload.every((c) => c.description.length <= 100 && (c.options || []).every((o) => o.description.length <= 100))
);
check("alle Commands sind vor Mitgliedern versteckt", payload.every((c) => c.default_member_permissions === "0"));
check("Fingerabdruck ist stabil", catalogVersion() === catalogVersion());

// --------------------------------------------------------------------------
section("Fehlende Secrets");
// --------------------------------------------------------------------------
resetFake(ready());
r = await post(cmd("ping"), { env: { DISCORD_PUBLIC_KEY: PUBLIC_KEY } });
check("nennt die fehlenden Secrets", r.content?.includes("DISCORD_BOT_TOKEN") && r.content?.includes("GIST_TOKEN"), r.content);

console.log(failed ? `\n${failed} FEHLER` : "\nALLES GRÜN");
process.exit(failed ? 1 : 0);
