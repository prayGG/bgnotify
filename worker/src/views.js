/**
 * Die nur-lesenden Ansichten: `/status`, `/track list`, `/account list`.
 *
 * Alles hier ist reines Rendern — gelesen wird aus dem privaten Gist
 * (Bestellstand) und dem öffentlichen Repo (Bot-Statistik, Kontonamen),
 * geschrieben wird nichts. Was diese Commands zeigen, ist damit exakt das,
 * was der Actions-Bot beim nächsten Lauf auch sieht.
 */

import { ago, clip, isTerminal, joinLines } from "./format.js";
import { loadAccountLabels, loadRepoState } from "./repo.js";

const COLOR_OK = 0x57f287;
const COLOR_IDLE = 0x95a5a6;
const COLOR_WARN = 0xfee75c;

// Bestellzustände, die als abgeschlossen gelten — wie `_TERMINAL_STATUS`
// in src/order_watch.py.
const ORDER_DONE = new Set(["completed", "cancelled", "refunded", "failed"]);

// Der Cronjob stößt den Bot alle 10 Minuten an. Bleibt er deutlich länger
// aus, stimmt etwas nicht — großzügig gerechnet, damit ein einzelner
// verpasster Lauf noch keinen Alarm auslöst.
const STALE_MINUTES = 45;

function truthy(v) {
  if (typeof v === "boolean") return v;
  return ["on", "an", "true", "yes", "y", "ja", "1"].includes(String(v).trim().toLowerCase());
}

/** An/Aus je Konto aus dem Gist-Feld `enabled` — gleiche Regeln wie im Bot. */
function accountEnabled(st, name) {
  const en = st.enabled;
  if (Array.isArray(en)) return en.includes(name);
  if (en && typeof en === "object") return truthy(en[name]);
  return en === true;
}

/** Sendungen aus beiden Quellen; bei gleichem Label schlägt der Handeintrag. */
function shipments(st) {
  const merged = { ...(st.auto_tracking || {}), ...(st.manual_tracking || {}) };
  const states = st.manual_tracking_state || {};
  return Object.entries(merged).map(([label, val]) => {
    const url = typeof val === "string" ? val : val?.url || val?.link || "";
    const state = states[label] || {};
    return {
      label,
      url,
      auto: !(st.manual_tracking || {})[label],
      status: state.status || "",
      lastCheck: state.last_check_at || "",
      done: isTerminal(state.status || ""),
    };
  });
}

// --------------------------------------------------------------------------

export async function statusView(st) {
  const repo = await loadRepoState();

  const stats = repo.bot_stats || {};
  const last = stats.last_check_at || "";
  const minutes = last ? (Date.now() - Date.parse(last)) / 60000 : Infinity;
  const healthy = minutes < STALE_MINUTES;

  const accounts = Object.keys(st.accounts || {});
  const active = accounts.filter((n) => accountEnabled(st, n));
  const ships = shipments(st);
  const unterwegs = ships.filter((s) => !s.done);

  const fields = [
    {
      name: "Bot",
      value: healthy
        ? `🟢⠀läuft⠀·⠀letzter Lauf ${ago(last) || "unbekannt"}`
        : `🔴⠀seit ${ago(last) || "unbekannt"} kein Lauf`,
    },
    {
      name: "Konten",
      value: accounts.length
        ? `${accounts.length} hinterlegt⠀·⠀**${active.length}** aktiv`
        : "keine hinterlegt",
    },
    {
      name: "Sendungen",
      value: ships.length
        ? `**${unterwegs.length}** unterwegs⠀·⠀${ships.length - unterwegs.length} zugestellt`
        : "keine verfolgt",
    },
  ];

  if (stats.total_checks) {
    fields.push({
      name: "Insgesamt",
      value: `${Number(stats.total_checks).toLocaleString("de-DE")} Läufe⠀·⠀${stats.total_restocks || 0} Restocks`,
    });
  }
  if (repo.error_report?.active) {
    fields.push({ name: "⚠️ Fehler", value: "Der letzte Lauf meldete Fehler — siehe Updates-Channel." });
  }

  return {
    author: { name: "✦⠀⠀status⠀⠀✦" },
    color: repo.error_report?.active ? COLOR_WARN : healthy ? COLOR_OK : COLOR_WARN,
    fields,
    timestamp: new Date().toISOString(),
  };
}

export async function trackListView(st) {
  const ships = shipments(st);
  if (!ships.length) {
    return {
      author: { name: "✦⠀⠀sendungen⠀⠀✦" },
      description: "Nichts in Verfolgung.\n\nSobald bei einer Bestellung ein Tracking-Link auftaucht, trägt der Bot ihn selbst ein.",
      color: COLOR_IDLE,
    };
  }

  // Unterwegs zuerst — das ist, wonach man schaut.
  ships.sort((a, b) => Number(a.done) - Number(b.done) || a.label.localeCompare(b.label));

  const lines = ships.map((s) => {
    const dot = s.done ? "✅" : "🚚";
    const herkunft = s.auto ? "automatisch" : "von Hand";
    const head = s.url ? `[${s.label}](${s.url})` : s.label;
    const stand = s.status ? clip(s.status, 90) : "_noch nicht abgefragt_";
    const wann = s.lastCheck ? `⠀·⠀${ago(s.lastCheck)}` : "";
    return `${dot}⠀**${head}**⠀·⠀_${herkunft}_\n⠀⠀⠀${stand}${wann}`;
  });

  return {
    author: { name: "✦⠀⠀sendungen⠀⠀✦" },
    description: joinLines(lines),
    color: ships.some((s) => !s.done) ? COLOR_OK : COLOR_IDLE,
    footer: { text: "zugestellte Sendungen werden nicht mehr abgefragt" },
    timestamp: new Date().toISOString(),
  };
}

export async function accountListView(st) {
  const labels = await loadAccountLabels();
  const names = Object.keys(st.accounts || {});

  if (!names.length) {
    return {
      author: { name: "✦⠀⠀konten⠀⠀✦" },
      description: "Keine Konten hinterlegt.",
      color: COLOR_IDLE,
    };
  }

  const lines = names.map((name) => {
    const acct = st.accounts[name] || {};
    const on = accountEnabled(st, name);
    const orders = Object.values(acct.orders || {});
    const offen = orders.filter((o) => !ORDER_DONE.has(o.status)).length;

    // Anzeigename aus config.yml, sonst der Schlüssel. Bestellnummern bleiben
    // bewusst draußen — die Zahl reicht, um zu wissen, ob etwas läuft.
    const titel = labels[name] || name;
    const teile = [
      acct.last_check_at ? `geprüft ${ago(acct.last_check_at)}` : "noch nie geprüft",
      offen ? `**${offen}** offen` : `${orders.length} erledigt`,
    ];
    return `${on ? "🟢" : "⚪"}⠀**${titel}**⠀·⠀${on ? "an" : "aus"}\n⠀⠀⠀${teile.join("⠀·⠀")}`;
  });

  return {
    author: { name: "✦⠀⠀konten⠀⠀✦" },
    description: joinLines(lines),
    color: COLOR_OK,
    footer: { text: "Konten schalten: /account enable · disable (kommt noch)" },
    timestamp: new Date().toISOString(),
  };
}
