/**
 * Die schreibenden Commands.
 *
 * Geschrieben wird ausschließlich nach `commands.json`, nie nach
 * `order-state.json` — dort arbeitet der Actions-Bot nach dem Muster
 * laden → ändern → speichern, ein Fremdschreiber würde Änderungen verlieren,
 * die zufällig dazwischen entstehen.
 *
 * Abgelegt wird ein **Wunschzustand**, keine Auftragsliste:
 *
 *     { "enabled":  { "a": "off" },
 *       "tracking": { "<name>": { "url": "…", "added_at": "…" } } }
 *
 * Der Bot legt das beim Lesen über seinen eigenen Stand. Das ist gegenüber
 * einer Warteschlange deutlich robuster: Ein Wunsch ist idempotent, muss nicht
 * quittiert werden, und ein doppelt ausgeführter Command richtet keinen Schaden
 * an. Aufträge braucht es erst für Dinge, die einen Browser erfordern
 * (Produkt aufnehmen, Login prüfen) — die kommen später.
 */

import { saveState } from "./gist.js";
import { actionsUrl, dispatchRun } from "./github.js";
import { deleteSecret, putSecrets } from "./secrets.js";

/**
 * Mindestabstand zwischen zwei per Command ausgelösten Läufen.
 *
 * Nicht als Schutz vor Missbrauch gedacht — wer die Rolle hat, darf das ja.
 * Es fängt den Reflex ab, bei ausbleibender Reaktion noch dreimal zu drücken:
 * Ein Lauf braucht ~30 Sekunden, und fünf gleichzeitige Läufe würden sich beim
 * Zurückschreiben von `state.json` gegenseitig überholen.
 */
const RUN_COOLDOWN_SECONDS = 90;

/** Hosts, deren Sendungsseiten `src/hermes.py` lesen kann. */
const HERMES_HOSTS = ["hermesworld.com", "myhermes.de"];

/**
 * Link prüfen und einen Anzeigenamen ableiten.
 *
 * Die Prüfung ist bewusst streng: Ein Link, den der Scraper nicht lesen kann,
 * würde sonst still eingetragen und erst Stunden später beim Bot-Lauf als
 * „nichts erkennbar" auffallen — weit weg von dem, der ihn eingetippt hat.
 */
export function parseTrackingLink(raw) {
  const url = (raw || "").trim();
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return { error: "Das ist keine gültige URL." };
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return { error: "Der Link muss mit `http://` oder `https://` anfangen." };
  }
  if (!HERMES_HOSTS.some((h) => parsed.hostname.endsWith(h))) {
    return {
      error: `Nur Hermes-Links werden unterstützt (${HERMES_HOSTS.join(", ")}). Andere Dienste kann der Bot nicht lesen.`,
    };
  }

  // Sendungsnummer aus Query oder Fragment — Hermes benutzt beides, je nachdem
  // über welchen Einstieg der Link erzeugt wurde.
  const id =
    parsed.searchParams.get("TrackID") ||
    parsed.searchParams.get("trackid") ||
    (parsed.hash || "").replace(/^#/, "");

  return { url, suggested: id ? `Sendung ${id.slice(-6)}` : "" };
}

/** Konto an- oder ausschalten. Gibt den Text für die Antwort zurück. */
export async function setAccountEnabled(env, state, name, on, known) {
  state.enabled ||= {};
  const vorher = state.enabled[name];
  state.enabled[name] = on ? "on" : "off";
  await saveState(env, state);

  const zusatz = known.includes(name)
    ? ""
    : `\n\n⚠️ Ein Konto **${name}** kenne ich nicht — der Schalter ist gesetzt, greift aber erst, wenn es das Konto gibt.`;
  const schon = vorher === (on ? "on" : "off") ? " (war schon so)" : "";

  return on
    ? `**${name}** ist **an**${schon}. Der nächste Lauf prüft es wieder.${zusatz}`
    : `**${name}** ist **aus**${schon}. Der Bot loggt sich dafür nicht mehr ein.${zusatz}`;
}

/** Sendung eintragen. */
export async function addTracking(env, state, label, url) {
  state.tracking ||= {};
  const vorhanden = state.tracking[label];

  // Bewusst OHNE die Discord-ID des Aufrufers: Ins Gist kommen keine
  // Discord-IDs, gespeichert wird höchstens der Name eines Secrets. Wer den
  // Command aufruft, bekommt die Meldungen über den Standard-Ping.
  state.tracking[label] = { url, added_at: new Date().toISOString() };
  await saveState(env, state);

  return vorhanden
    ? `**${label}** zeigt jetzt auf den neuen Link. Der Stand wurde zurückgesetzt, der Verlauf kommt beim nächsten Lauf frisch.`
    : `**${label}** wird ab dem nächsten Lauf verfolgt. Bei jedem neuen Ereignis kommt eine Meldung — bei Zustellung hört der Bot von selbst auf.`;
}

// --------------------------------------------------------------------------
// Konto-Slots
// --------------------------------------------------------------------------

/**
 * Vorverdrahtete Plätze für selbst hinterlegte Konten.
 *
 * Ein neues Secret nützt nichts, wenn der Workflow es nicht durchreicht — und
 * Actions kann Secrets nicht dynamisch auflisten. Deshalb stehen die Namen fest
 * in `main.yml`, und `/account add` belegt den nächsten freien. Harte Grenze bei
 * vier zusätzlichen Konten; wer mehr will, trägt in `main.yml` weitere Plätze
 * ein und erweitert diese Liste.
 *
 * Die Alternative `toJSON(secrets)` kippt ALLE Secrets in eine Variable,
 * inklusive Gist- und GitHub-Token — nicht machen.
 */
export const SLOTS = [3, 4, 5, 6];

export const slotSecrets = (n) => ({
  user: `BG_USERNAME_${n}`,
  pass: `BG_PASSWORD_${n}`,
  ping: `DISCORDID_${n}`,
});

/** Nächster freier Platz, oder null wenn alle belegt sind. */
export function freeSlot(state) {
  const belegt = new Set(Object.keys(state.accounts || {}).map(Number));
  return SLOTS.find((n) => !belegt.has(n)) ?? null;
}

/**
 * Konto hinterlegen: drei Secrets setzen, Platz im Gist vermerken.
 *
 * Ins Gist kommen nur Anzeigename und Platznummer — keine Zugangsdaten, keine
 * Discord-ID. Die ID des Aufrufers wird als Secret abgelegt, damit ihn der Bot
 * bei seinen Bestellungen anpingen kann, ohne dass sie irgendwo gespeichert
 * steht, wo sie nicht hingehört.
 */
export async function addAccount(env, state, slot, label, user, pass, discordId) {
  const s = slotSecrets(slot);
  await putSecrets(env, { [s.user]: user, [s.pass]: pass, [s.ping]: discordId });

  state.accounts ||= {};
  state.accounts[String(slot)] = {
    label,
    added_at: new Date().toISOString(),
  };
  await saveState(env, state);
}

/** Konto entfernen: Secrets löschen, Platz freigeben. */
export async function removeAccount(env, state, slot) {
  const s = slotSecrets(slot);
  for (const name of [s.user, s.pass, s.ping]) {
    await deleteSecret(env, name);
  }
  delete state.accounts?.[String(slot)];
  // Auch den An/Aus-Schalter aufräumen, sonst bliebe eine Leiche stehen, die
  // beim nächsten belegten Platz derselben Nummer plötzlich wieder gälte.
  if (state.enabled) delete state.enabled[`s${slot}`];
  await saveState(env, state);
}

/** Einen Bot-Lauf anstoßen. */
export async function triggerRun(env, state) {
  const last = Date.parse(state.last_run_at || "");
  const wartet = Number.isNaN(last) ? 0 : RUN_COOLDOWN_SECONDS - Math.floor((Date.now() - last) / 1000);
  if (wartet > 0) {
    return `Gerade eben schon angestoßen — noch **${wartet} s** warten.\n\nEin Lauf braucht rund eine halbe Minute. Die Meldungen kommen wie immer in die Channels.`;
  }

  await dispatchRun(env);

  // Erst nach dem erfolgreichen Anstoßen merken: Sonst würde ein fehl-
  // geschlagener Versuch die Sperre auslösen und man käme 90 s nicht wieder ran.
  state.last_run_at = new Date().toISOString();
  await saveState(env, state);

  return `Lauf angestoßen. Dauert rund eine halbe Minute, die Meldungen kommen in die Channels.\n\n[→⠀⠀bei GitHub zusehen](${actionsUrl()})`;
}

/** Sendung austragen. */
export async function removeTracking(env, state, label) {
  if (!state.tracking?.[label]) {
    return { ok: false, text: `**${label}** steht nicht in der Liste der von Hand eingetragenen Sendungen.` };
  }
  delete state.tracking[label];
  await saveState(env, state);
  return { ok: true, text: `**${label}** wird nicht mehr verfolgt.` };
}
