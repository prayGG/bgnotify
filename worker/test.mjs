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
import { buildEmbed } from "./src/panel.js";
import nacl from "tweetnacl";
import sealedbox from "tweetnacl-sealedbox-js";

// Echtes Schluesselpaar: so beweist der Test, dass die Werte wirklich
// versiegelt ankommen und nicht nur "irgendwie anders aussehen".
const SEAL_KP = nacl.box.keyPair();
const SEAL_PUBLIC_B64 = btoa(String.fromCharCode(...SEAL_KP.publicKey));

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
    dispatched: 0,
    dispatchStatus: 0,
    secrets: {},
    deleted: [],
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
    // Secrets-API. Der oeffentliche Schluessel ist ein echtes NaCl-Schluesselpaar,
    // damit der Test wirklich pruefen kann, dass nichts im Klartext rausgeht.
    if (method === "GET" && path.endsWith("/actions/secrets/public-key")) {
      return jsonRes({ key: SEAL_PUBLIC_B64, key_id: "1" });
    }
    if (method === "PUT" && path.includes("/actions/secrets/")) {
      fake.secrets[path.split("/").pop()] = JSON.parse(init.body).encrypted_value;
      return new Response(null, { status: 201 });
    }
    if (method === "DELETE" && path.includes("/actions/secrets/")) {
      fake.deleted.push(path.split("/").pop());
      return new Response(null, { status: 204 });
    }
    if (method === "POST" && path.includes("/actions/workflows/")) {
      if (fake.dispatchStatus) return new Response("", { status: fake.dispatchStatus });
      fake.dispatched++;
      return new Response(null, { status: 204 });
    }
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

const cmd = (path, { roles = [ROLE], userId = OTHER, dm = false, args = {} } = {}) => {
  const [name, sub] = path.split(" ");
  const opts = Object.entries(args).map(([k, v]) => ({ name: k, value: v, type: 3 }));
  const data = { name };
  if (sub) data.options = [{ type: 1, name: sub, ...(opts.length ? { options: opts } : {}) }];
  else if (opts.length) data.options = opts;
  return {
    type: 2,
    application_id: "app-1",
    token: "interaction-token",
    channel_id: CHANNEL,
    ...(dm ? { user: { id: userId } } : { guild_id: GUILD, member: { user: { id: userId }, roles } }),
    data,
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
section("/account enable · disable");
// --------------------------------------------------------------------------
resetFake({ ...ready(), orders: { accounts: { a: {}, b: {} }, enabled: { a: "on", b: "on" } } });
r = await post(cmd("account disable", { args: { konto: "a" } }));
check("schreibt den Wunsch nach commands.json", fake.commands.enabled?.a === "off", JSON.stringify(fake.commands.enabled));
check("fasst order-state.json NICHT an", fake.patchedFiles.join(",") === "commands.json", fake.patchedFiles.join(","));
check("meldet es verständlich", r.content?.includes("aus"), r.content);

r = await post(cmd("account list"));
check("/account list zeigt sofort „aus“", r.embed.description.includes("⚪"), r.embed.description.split("\n")[0]);

r = await post(cmd("account enable", { args: { konto: "a" } }));
check("wieder einschalten geht", fake.commands.enabled?.a === "on");

r = await post(cmd("account disable", { args: { konto: "gibtsnicht" } }));
check("unbekanntes Konto → Warnung, aber gesetzt", r.content?.includes("kenne ich nicht"), r.content);

// --------------------------------------------------------------------------
section("/track add · remove");
// --------------------------------------------------------------------------
resetFake(ready());
r = await post(cmd("track add", { args: { link: "https://example.com/paket" } }));
check("fremder Dienst → abgelehnt", r.content?.includes("Nur Hermes-Links"), r.content);
check("… und nichts geschrieben", Object.keys(fake.commands.tracking || {}).length === 0);

r = await post(cmd("track add", { args: { link: "nicht mal eine url" } }));
check("Unsinn → abgelehnt", r.content?.includes("keine gültige URL"), r.content);

r = await post(cmd("track add", {
  args: { link: "https://www.myhermes.de/x?TrackID=H1023311266211701051", name: "mave" },
}));
check("Hermes-Link wird eingetragen", fake.commands.tracking?.mave?.url.includes("H1023311266211701051"));
check("KEINE Discord-ID im Gist", !JSON.stringify(fake.commands.tracking).includes(OTHER), JSON.stringify(fake.commands.tracking.mave));
check("schreibt NUR commands.json", fake.patchedFiles.join(",") === "commands.json");

r = await post(cmd("track list"));
check("/track list zeigt ihn sofort", r.embed.description.includes("mave") && r.embed.description.includes("via Discord"), r.embed.description);

r = await post(cmd("track add", { args: { link: "https://tracking.hermesworld.com/?TrackID=H999888777666" } }));
const abgeleitet = Object.keys(fake.commands.tracking).find((k) => k !== "mave");
check("ohne Namen wird einer abgeleitet", Boolean(abgeleitet), abgeleitet);

r = await post(cmd("track remove", { args: { name: "mave" } }));
check("entfernen klappt", !fake.commands.tracking.mave, r.content);

r = await post(cmd("track remove", { args: { name: "gibtsnicht" } }));
check("unbekannter Name → ehrliche Absage", r.content?.includes("steht nicht in der Liste"), r.content);

// --------------------------------------------------------------------------
section("/run");
// --------------------------------------------------------------------------
resetFake(ready());
r = await post(cmd("run"), { env: { ...FULL_ENV, GITHUB_TOKEN: "gh-token" } });
check("stößt den Lauf an", fake.dispatched === 1, `${fake.dispatched} Dispatches`);
check("antwortet deferred", r.body.type === 5);
check("meldet es mit Link", fake.followUp.includes("angestoßen") && fake.followUp.includes("github.com"), fake.followUp.split("\n")[0]);
check("merkt sich den Zeitpunkt", Boolean(fake.commands.last_run_at));

r = await post(cmd("run"), { env: { ...FULL_ENV, GITHUB_TOKEN: "gh-token" } });
check("zweiter Aufruf sofort danach → Sperre", fake.followUp.includes("warten"), fake.followUp.split("\n")[0]);
check("… und stößt NICHT nochmal an", fake.dispatched === 1, `${fake.dispatched} Dispatches`);

resetFake(ready());
r = await post(cmd("run"));
check("ohne GITHUB_TOKEN → sagt das klar", r.content?.includes("GITHUB_TOKEN"), r.content);

// Fehlende Rechte dürfen die Sperre NICHT auslösen, sonst kommt man 90 s nicht wieder ran.
resetFake({ ...ready(), dispatchStatus: 404 });
r = await post(cmd("run"), { env: { ...FULL_ENV, GITHUB_TOKEN: "gh-token" } });
check("fehlende Rechte werden erklärt", fake.followUp.includes("Actions: read and write"), fake.followUp);
check("… und sperren nicht", !fake.commands.last_run_at);

// --------------------------------------------------------------------------
section("/account add");
// --------------------------------------------------------------------------
const GH_ENV = { ...FULL_ENV, GITHUB_TOKEN: "gh-token" };

resetFake(ready());
r = await post(cmd("account add"), { env: GH_ENV });
check("antwortet mit einem Modal (Typ 9)", r.body.type === 9, `type=${r.body.type}`);
check("fragt Name, Benutzer, Passwort", JSON.stringify(r.body.data.components).match(/custom_id":"(label|user|pass)"/g)?.length === 3);
check("Passwort ist KEINE Command-Option", !JSON.stringify(toDiscordPayload()).includes("passwor"));

const modal = (werte, { roles = [ROLE] } = {}) => ({
  type: 5,
  application_id: "app-1",
  token: "interaction-token",
  guild_id: GUILD,
  member: { user: { id: OTHER }, roles },
  data: {
    custom_id: "account_add",
    components: Object.entries(werte).map(([k, v]) => ({
      type: 1, components: [{ type: 4, custom_id: k, value: v }],
    })),
  },
});

resetFake(ready());
r = await post(modal({ label: "kollege", user: "mave@x.de", pass: "geheim123" }), { env: GH_ENV });
check("legt drei Secrets an", Object.keys(fake.secrets).length === 3, Object.keys(fake.secrets).join(", "));
check("belegt Platz 3", Boolean(fake.commands.accounts?.["3"]), JSON.stringify(fake.commands.accounts));
check("merkt sich nur den Anzeigenamen", JSON.stringify(fake.commands.accounts["3"]).includes("kollege"));

const gespeichert = JSON.stringify(fake.commands);
check("KEIN Passwort im Gist", !gespeichert.includes("geheim123"), gespeichert.slice(0, 80));
check("KEIN Benutzername im Gist", !gespeichert.includes("mave@x.de"));
check("KEINE Discord-ID im Gist", !gespeichert.includes(OTHER));
check("Discord-ID liegt als Secret", fake.secrets.DISCORDID_3 !== undefined);

const roh = Object.values(fake.secrets).join("|");
check("Secrets gehen NUR verschlüsselt raus", !roh.includes("geheim123") && !roh.includes("mave@x.de"), roh.slice(0, 60));

// Der eigentliche Beweis: mit dem privaten Schlüssel muss GENAU das Original
// wieder herauskommen. „Sieht anders aus" wäre auch bei kaputter
// Verschlüsselung wahr — GitHub könnte den Wert dann aber nie benutzen.
const entsiegelt = (b64) => {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const auf = sealedbox.open(bytes, SEAL_KP.publicKey, SEAL_KP.secretKey);
  return auf ? new TextDecoder().decode(auf) : null;
};
check("BG_PASSWORD_3 entschlüsselt zum Original", entsiegelt(fake.secrets.BG_PASSWORD_3) === "geheim123", String(entsiegelt(fake.secrets.BG_PASSWORD_3)));
check("BG_USERNAME_3 entschlüsselt zum Original", entsiegelt(fake.secrets.BG_USERNAME_3) === "mave@x.de");
check("DISCORDID_3 enthält die ID des Aufrufers", entsiegelt(fake.secrets.DISCORDID_3) === OTHER);

check("bittet um Login-Prüfung", fake.commands.accounts["3"].verify === true);
check("stößt dafür sofort einen Lauf an", fake.dispatched === 1, `${fake.dispatched} Dispatches`);
check("sagt das auch", fake.followUp.includes("wird gerade geprüft"), fake.followUp.split("\n").pop());

// Ohne Actions-Recht muss das Konto trotzdem angelegt sein — nur der Hinweis
// ändert sich. Alles andere hieße: Passwort eingetippt, nichts passiert.
resetFake({ ...ready(), dispatchStatus: 404 });
r = await post(modal({ label: "ohnelauf", user: "u", pass: "p" }), { env: GH_ENV });
check("Lauf scheitert → Konto trotzdem angelegt", Boolean(fake.commands.accounts?.["3"]), JSON.stringify(fake.commands.accounts));
check("… und der Hinweis wird abgeschwächt", fake.followUp.includes("nächste Lauf"), fake.followUp.split("\n").pop());

resetFake(ready());
r = await post(modal({ label: "erster", user: "u1", pass: "p1" }), { env: GH_ENV });
r = await post(modal({ label: "zweiter", user: "u2", pass: "p2" }), { env: GH_ENV });
check("nächstes Konto nimmt Platz 4", Boolean(fake.commands.accounts?.["4"]));

r = await post(modal({ label: "", user: "u", pass: "p" }), { env: GH_ENV });
check("leeres Feld → freundlicher Abbruch", r.content?.includes("fehlt ein Feld"), r.content);

r = await post(modal({ label: "x", user: "u", pass: "p" }, { roles: [] }), { env: GH_ENV });
check("Formular ohne Rolle → abgelehnt", r.content?.includes("Rolle"), r.content);

resetFake(ready());
fake.commands.accounts = { 3: {}, 4: {}, 5: {}, 6: {} };
r = await post(cmd("account add"), { env: GH_ENV });
check("alle Plätze belegt → sagt das statt Modal", r.content?.includes("belegt"), r.content);

// --------------------------------------------------------------------------
section("/account remove");
// --------------------------------------------------------------------------
resetFake(ready());
fake.commands.accounts = { 3: { label: "kollege" } };
fake.commands.enabled = { s3: "on" };
r = await post(cmd("account remove", { args: { konto: "3" } }), { env: GH_ENV });
check("löscht alle drei Secrets", fake.deleted.length === 3, fake.deleted.join(", "));
check("gibt den Platz frei", !fake.commands.accounts["3"]);
check("räumt den An/Aus-Schalter mit weg", !fake.commands.enabled.s3);

r = await post(cmd("account remove", { args: { konto: "9" } }), { env: GH_ENV });
check("fest verdrahtetes Konto → Absage", fake.followUp.includes("config.yml"), fake.followUp);

// --------------------------------------------------------------------------
section("Autocomplete");
// --------------------------------------------------------------------------
resetFake({
  ...ready(),
  configYml: "accounts:\n    - name: a\n      label: pray\n    - name: b\n      label: mave\n",
  orders: { accounts: { a: {}, b: {} } },
});
r = await post({ ...cmd("account enable"), type: 4, data: { name: "account", options: [{ type: 1, name: "enable", options: [{ name: "konto", value: "", focused: true }] }] } });
check("antwortet mit Typ 8", r.body.type === 8, `type=${r.body.type}`);
check("schlägt beide Konten vor", r.body.data.choices.length === 2, JSON.stringify(r.body.data.choices));
check("zeigt den Anzeigenamen", r.body.data.choices[0].name.includes("pray"), r.body.data.choices[0].name);
check("liefert aber den Schlüssel", r.body.data.choices[0].value === "a");

r = await post({ ...cmd("account enable"), type: 4, data: { name: "account", options: [{ type: 1, name: "enable", options: [{ name: "konto", value: "mav", focused: true }] }] } });
check("filtert nach Eingabe", r.body.data.choices.length === 1 && r.body.data.choices[0].value === "b");

// Selbst hinterlegte Konten tragen ihren Namen in commands.json, nicht in
// config.yml. Wer nur die eine Quelle liest, zeigt "s3" statt "kollege".
resetFake({
  ...ready(),
  configYml: "accounts:\n    - name: a\n      label: pray\n",
  orders: { accounts: { a: {}, s3: {} } },
});
fake.commands.accounts = { 3: { label: "kollege" } };
r = await post({ ...cmd("account enable"), type: 4, data: { name: "account", options: [{ type: 1, name: "enable", options: [{ name: "konto", value: "", focused: true }] }] } });
const namen = r.body.data.choices.map((c) => c.name);
check("selbst hinterlegtes Konto zeigt seinen Namen", namen.includes("kollege (s3)"), namen.join(", "));

r = await post(cmd("account list"));
check("/account list ebenso", r.embed.description.includes("kollege"), r.embed.description.split("\n").join(" | "));

const ohneRolle = { ...cmd("account enable", { roles: [] }), type: 4, data: { name: "account", options: [{ type: 1, name: "enable", options: [{ name: "konto", value: "", focused: true }] }] } };
r = await post(ohneRolle);
check("ohne Rolle → leere Liste statt Verrat", r.body.data.choices.length === 0);

resetFake(ready());
fake.commands.tracking = { mave: { url: "https://x" }, "pray #1": { url: "https://y" } };
r = await post({ ...cmd("track remove"), type: 4, data: { name: "track", options: [{ type: 1, name: "remove", options: [{ name: "name", value: "", focused: true }] }] } });
check("schlägt eingetragene Sendungen vor", r.body.data.choices.length === 2, JSON.stringify(r.body.data.choices.map((c) => c.value)));

// --------------------------------------------------------------------------
section("/product");
// --------------------------------------------------------------------------
const PROD = "https://bgpharmadrugs.to/product/peptides/";

resetFake(ready());
r = await post(cmd("product add", { args: { link: "https://example.com/product/x" } }));
check("fremder Shop → abgelehnt", r.content?.includes("bgpharmadrugs.to"), r.content);

r = await post(cmd("product add", { args: { link: "https://bgpharmadrugs.to/shop/" } }));
check("keine Produktseite → abgelehnt", r.content?.includes("/product/"), r.content);

// Erster Aufruf: Seite unbekannt → Auftrag anlegen und Lauf anstoßen.
resetFake(ready());
r = await post(cmd("product add", { args: { link: PROD + "?attr=x#frag" } }), { env: GH_ENV });
check("unbekannte Seite → zum Einlesen angemeldet", Boolean(fake.commands.scans?.[PROD]), JSON.stringify(fake.commands.scans));
check("Link wird normalisiert (ohne Query/Fragment)", Object.keys(fake.commands.scans)[0] === PROD);
check("stößt dafür einen Lauf an", fake.dispatched === 1);
check("nimmt NOCH NICHTS in Beobachtung", !fake.commands.products);

// Zweiter Aufruf, nachdem der Bot eingelesen hat.
resetFake({
  ...ready(),
  orders: { product_scans: { [PROD]: { title: "Peptides and HGH", simple: false, variants: ["BPC157 10mg", "TB500 10mg"] } } },
});
r = await post(cmd("product add", { args: { link: PROD } }));
check("eingelesen, aber ohne Variante → bittet um Auswahl", r.content?.includes("2 Varianten"), r.content);

r = await post(cmd("product add", { args: { link: PROD, variante: "Gibtsnicht 5mg" } }));
check("erfundene Variante → abgelehnt", r.content?.includes("gibt es auf der Seite nicht"), r.content);
check("… und nichts aufgenommen", !fake.commands.products);

r = await post(cmd("product add", { args: { link: PROD, variante: "BPC157 10mg" } }));
check("echte Variante → aufgenommen", Boolean(fake.commands.products), JSON.stringify(fake.commands.products));
check("merkt den Wortlaut EXAKT", Object.values(fake.commands.products)[0].variants[0] === "BPC157 10mg");
check("schreibt NUR commands.json", fake.patchedFiles.join(",") === "commands.json");

// Einzelprodukt: keine Auswahl nötig.
resetFake({
  ...ready(),
  orders: { product_scans: { [PROD]: { title: "Roaccutane 20 mg", simple: true, variants: [] } } },
});
r = await post(cmd("product add", { args: { link: PROD } }));
check("Einzelprodukt → direkt aufgenommen", r.content?.includes("wird ab jetzt beobachtet"), r.content);

// Nicht lesbare Seite.
resetFake({ ...ready(), orders: { product_scans: { [PROD]: { error: "HTTP 404" } } } });
r = await post(cmd("product add", { args: { link: PROD } }));
check("Seite war nicht lesbar → sagt warum", r.content?.includes("404"), r.content);

// Autocomplete
resetFake({
  ...ready(),
  orders: { product_scans: { [PROD]: { title: "Peptides and HGH", simple: false, variants: ["BPC157 10mg", "TB500 10mg"] } } },
});
const ac = (sub, opts) => ({
  ...cmd(`product ${sub}`),
  type: 4,
  data: { name: "product", options: [{ type: 1, name: sub, options: opts }] },
});
r = await post(ac("add", [{ name: "link", value: "", focused: true }]));
check("Autocomplete schlägt eingelesene Seiten vor", r.body.data.choices[0]?.value === PROD, JSON.stringify(r.body.data.choices));
check("… mit dem Produkttitel", r.body.data.choices[0]?.name === "Peptides and HGH");

r = await post(ac("add", [{ name: "link", value: PROD }, { name: "variante", value: "bpc", focused: true }]));
check("Varianten-Autocomplete filtert", r.body.data.choices.length === 1 && r.body.data.choices[0].value === "BPC157 10mg", JSON.stringify(r.body.data.choices));

// /product list
resetFake(ready());
r = await post(cmd("product list"));
check("leere Liste erklärt config.yml", r.embed.description.includes("config.yml"));

resetFake({ ...ready(), orders: { product_scans: {} } });
fake.commands.products = { k1: { url: PROD, name: "BPC157 10mg" } };
fake.commands.scans = { "https://bgpharmadrugs.to/product/neu/": {} };
r = await post(cmd("product list"));
check("zeigt Aufgenommenes", r.embed.description.includes("BPC157 10mg"));
check("zeigt auch Wartendes", r.embed.description.includes("wird beim nächsten Lauf eingelesen"), r.embed.description);

// Umbenennen darf NUR die Anzeige treffen. Wanderte der Wortlaut mit, liefe der
// Abgleich gegen das Dropdown der Seite ins Leere — das Produkt stuende weiter
// im Dashboard und waere nie wieder auf Lager, ohne dass etwas nach Fehler aussieht.
fake.commands.products.k1.variants = ["Azelaic Acid 20% 30 gr cream"];
r = await post(cmd("product rename", { args: { produkt: "k1", name: "Azelaic 20% 30g" } }));
check("umbenennen klappt", fake.commands.products.k1.label === "Azelaic 20% 30g", r.content);
check("… Match-String unangetastet",
  fake.commands.products.k1.variants[0] === "Azelaic Acid 20% 30 gr cream",
  String(fake.commands.products.k1.variants));
r = await post(cmd("product rename", { args: { produkt: "k1", name: "   " } }));
check("leerer Name setzt zurueck", !("label" in fake.commands.products.k1), r.content);
r = await post(cmd("product rename", { args: { produkt: "gibtsnicht", name: "X" } }));
check("fest gepflegtes → Absage beim Umbenennen", r.content?.includes("config.yml"), r.content);

r = await post(cmd("product remove", { args: { produkt: "k1" } }));
check("entfernen klappt", !fake.commands.products.k1, r.content);
r = await post(cmd("product remove", { args: { produkt: "gibtsnicht" } }));
check("fest gepflegtes → Absage", r.content?.includes("config.yml"), r.content);

// --------------------------------------------------------------------------
section("Discord-Grenzen der Übersicht");
// --------------------------------------------------------------------------
// Diese Grenzen kürzt Discord nicht, es lehnt die ganze Nachricht mit HTTP 400
// ab. Der Fehler waechst still mit: Bis /product dazukam, passte alles in EIN
// Feld — danach lag das Panel liegen, ohne dass vorher irgendetwas warnte.
{
  const e = buildEmbed();
  const felder = e.fields || [];
  const zuLang = felder.filter((f) => f.value.length > 1024);
  check(
    "jedes Feld ≤ 1024 Zeichen",
    zuLang.length === 0,
    zuLang.map((f) => `${f.name.trim()}=${f.value.length}`).join(", ") ||
      `längstes ${Math.max(...felder.map((f) => f.value.length))}`
  );
  check("höchstens 25 Felder", felder.length <= 25, `${felder.length}`);
  check("Feldnamen ≤ 256", felder.every((f) => f.name.length <= 256));
  check("Titel ≤ 256", (e.title || "").length <= 256);
  check("Beschreibung ≤ 4096", (e.description || "").length <= 4096);

  const gesamt =
    (e.title || "").length + (e.description || "").length +
    (e.footer?.text || "").length +
    felder.reduce((n, f) => n + f.name.length + f.value.length, 0);
  check("Gesamtlänge ≤ 6000", gesamt <= 6000, `${gesamt}`);

  // Beim Umbruch darf nichts verlorengehen — das waere der stille Schaden.
  const text = felder.map((f) => f.value).join("\n");
  const fehlend = flatten().filter((c) => !text.includes(`/${c.path}`));
  check("kein Command geht beim Umbruch verloren", fehlend.length === 0, fehlend.map((c) => c.path).join(", "));
  check("wird tatsächlich auf mehrere Felder verteilt", felder.length > 2, `${felder.length} Felder`);
}

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
