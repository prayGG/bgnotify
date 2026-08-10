/**
 * Die nur-lesenden Ansichten: `/status`, `/track list`, `/account list`.
 *
 * Alles hier ist reines Rendern — gelesen wird aus dem privaten Gist
 * (Bestellstand) und dem öffentlichen Repo (Bot-Statistik, Kontonamen),
 * geschrieben wird nichts. Was diese Commands zeigen, ist damit exakt das,
 * was der Actions-Bot beim nächsten Lauf auch sieht.
 */

import { ago, clip, isTerminal, joinLines } from "./format.js";
import { slotLabels } from "./actions.js";
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

/**
 * An/Aus je Konto — gleiche Regeln wie im Bot, plus der über Discord gesetzte
 * Wunschzustand aus `commands.json`. Der hat Vorrang: Er ist das Neuere, und
 * der Bot legt ihn beim nächsten Lauf genauso darüber. Ohne diesen Vorrang
 * zeigte `/account list` direkt nach `/account disable` noch „an" — und niemand
 * wüsste, ob der Command angekommen ist.
 */
function accountEnabled(st, cmd, name) {
  // Der Bot schaltet ein Konto nach der Zustellung selbst ab und legt dafür
  // `_auto_off` in seinen Stand — `commands.json` kann er nicht zurückschreiben,
  // die gehört dem Worker. Ohne diese Zeile stünde hier „an", während sich der
  // Bot längst nicht mehr einloggt: die Sorte Anzeige, der man danach nichts
  // mehr glaubt.
  if (st.accounts?.[name]?._auto_off) return false;

  const wunsch = cmd?.enabled?.[name];
  if (wunsch !== undefined) return truthy(wunsch);

  const en = st.enabled;
  if (Array.isArray(en)) return en.includes(name);
  if (en && typeof en === "object") return truthy(en[name]);
  return en === true;
}

/**
 * Sendungen aus allen drei Quellen. Reihenfolge = Vorrang: automatisch
 * übernommene zuerst, dann die von Hand im Gist, zuletzt die über Discord
 * eingetragenen — die sind die jüngsten.
 */
function shipments(st, cmd) {
  const viaDiscord = cmd?.tracking || {};
  const merged = { ...(st.auto_tracking || {}), ...(st.manual_tracking || {}), ...viaDiscord };
  const states = st.manual_tracking_state || {};
  return Object.entries(merged).map(([label, val]) => {
    const url = typeof val === "string" ? val : val?.url || val?.link || "";
    const state = states[label] || {};
    const auto = !(st.manual_tracking || {})[label] && !viaDiscord[label];
    return {
      label,
      url,
      herkunft: viaDiscord[label] ? "via Discord" : auto ? "automatisch" : "von Hand",
      status: state.status || "",
      lastCheck: state.last_check_at || "",
      done: isTerminal(state.status || ""),
    };
  });
}

// --------------------------------------------------------------------------

export async function statusView(st, cmd = {}) {
  const repo = await loadRepoState();

  const stats = repo.bot_stats || {};
  const last = stats.last_check_at || "";
  const minutes = last ? (Date.now() - Date.parse(last)) / 60000 : Infinity;
  const healthy = minutes < STALE_MINUTES;

  const accounts = Object.keys(st.accounts || {});
  const active = accounts.filter((n) => accountEnabled(st, cmd, n));
  const ships = shipments(st, cmd);
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

export async function trackListView(st, cmd = {}) {
  const ships = shipments(st, cmd);
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
    const head = s.url ? `[${s.label}](${s.url})` : s.label;
    const stand = s.status ? clip(s.status, 90) : "_noch nicht abgefragt_";
    const wann = s.lastCheck ? `⠀·⠀${ago(s.lastCheck)}` : "";
    return `${dot}⠀**${head}**⠀·⠀_${s.herkunft}_\n⠀⠀⠀${stand}${wann}`;
  });

  return {
    author: { name: "✦⠀⠀sendungen⠀⠀✦" },
    description: joinLines(lines),
    color: ships.some((s) => !s.done) ? COLOR_OK : COLOR_IDLE,
    footer: { text: "zugestellte Sendungen werden nicht mehr abgefragt" },
    timestamp: new Date().toISOString(),
  };
}

export async function accountListView(st, cmd = {}) {
  // Fest verdrahtete Konten aus config.yml, selbst hinterlegte aus dem Gist.
  const labels = { ...(await loadAccountLabels()), ...slotLabels(cmd) };
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
    const on = accountEnabled(st, cmd, name);
    const orders = Object.values(acct.orders || {});
    const offen = orders.filter((o) => !ORDER_DONE.has(o.status)).length;

    // Ging der letzte Abruf schief, ist DAS die Nachricht. Vorher stand hier
    // nur „geprüft vor 5 min" — und das sah genauso aus, ob der Login stand
    // oder das Passwort seit Wochen falsch ist. Wer eine Kontoliste aufruft,
    // will genau das wissen.
    const kaputt = acct.login_ok === false;
    const ruht = Boolean(acct._auto_off);

    // Anzeigename aus config.yml, sonst der Schlüssel. Bestellnummern bleiben
    // bewusst draußen — die Zahl reicht, um zu wissen, ob etwas läuft.
    const titel = labels[name] || name;
    const teile = [
      acct.last_check_at ? `geprüft ${ago(acct.last_check_at)}` : "noch nie geprüft",
      offen ? `**${offen}** offen` : `${orders.length} erledigt`,
    ];
    if (kaputt) teile.push("**Abruf fehlgeschlagen**");
    const dot = kaputt ? "❌" : ruht ? "💤" : on ? "🟢" : "⚪";
    // „ruht" statt „aus": Ausgeschaltet hat es niemand — es ist fertig. Wer das
    // verwechselt, sucht den Schalter, den er nie umgelegt hat.
    const zustand = ruht ? "ruht (alles zugestellt)" : on ? "an" : "aus";
    return `${dot}⠀**${titel}**⠀·⠀${zustand}\n⠀⠀⠀${teile.join("⠀·⠀")}`;
  });

  return {
    author: { name: "✦⠀⠀konten⠀⠀✦" },
    description: joinLines(lines),
    color: COLOR_OK,
    footer: { text: "schalten mit /account enable · /account disable" },
    timestamp: new Date().toISOString(),
  };
}

export async function productListView(st, cmd = {}) {
  const ausCommands = Object.entries(cmd.products || {});
  const eingelesen = st.product_scans || {};
  const offen = Object.keys(cmd.scans || {}).filter((u) => !eingelesen[u]);

  const lines = ausCommands.map(([, p]) => `🔎⠀**[${p.name}](${p.url})**\n⠀⠀⠀_per Command aufgenommen_`);

  // Was noch aufs Einlesen wartet, gehört sichtbar dazu — sonst wirkt ein
  // gerade angemeldetes Produkt, als wäre es verschluckt worden.
  for (const url of offen) {
    lines.push(`⏳⠀**${url.replace(/^https?:\/\/[^/]+/, "")}**\n⠀⠀⠀_wird beim nächsten Lauf eingelesen_`);
  }

  if (!lines.length) {
    return {
      author: { name: "✦⠀⠀produkte⠀⠀✦" },
      description:
        "Hier stehen nur die per `/product add` aufgenommenen.\n\n" +
        "Die fest gepflegten liegen in `config.yml` — dort stehen Erklärkommentare zu jedem " +
        "Eintrag, die ein Programm beim Neuschreiben wegwerfen würde. Deshalb bleiben sie von Hand.",
      color: COLOR_IDLE,
    };
  }

  return {
    author: { name: "✦⠀⠀produkte⠀⠀✦" },
    description: joinLines(lines),
    color: COLOR_OK,
    footer: { text: "die fest gepflegten stehen in config.yml" },
    timestamp: new Date().toISOString(),
  };
}
