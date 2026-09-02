/**
 * Lesezugriff auf das öffentliche Repo — ohne Token, weil dort nichts Geheimes
 * liegt (genau deshalb steht der Bestellstand ja im privaten Gist).
 *
 * Gebraucht für zwei Dinge, die der Bot nur im Repo führt: den Zeitpunkt des
 * letzten Laufs (`state.json`) und die Anzeigenamen der Konten (`config.yml`).
 */

const RAW = "https://raw.githubusercontent.com/praygoated/bgnotify/main";

/** `state.json` — Bot-Statistiken, Fehlerzustand. Leer statt Fehler, wenn nicht lesbar. */
export async function loadRepoState() {
  try {
    const res = await fetch(`${RAW}/state.json`, {
      headers: { "user-agent": "bgnotify-commands-worker" },
      cf: { cacheTtl: 60 }, // der Bot schreibt alle 10 min — häufiger fragen bringt nichts
    });
    if (!res.ok) return {};
    return await res.json();
  } catch {
    return {};
  }
}

/**
 * Kontoschlüssel → Anzeigename aus `config.yml`, also etwa `a → haupt`.
 *
 * Bewusst per Regex statt mit einem YAML-Parser: Gebraucht werden genau zwei
 * Felder, und dafür lohnt keine Abhängigkeit im Worker-Bundle. Findet sich
 * nichts, ist das kein Fehler — die Anzeige fällt dann auf den Schlüssel
 * zurück, und das ist bloß hässlicher, nicht falsch.
 */
export async function loadAccountLabels() {
  try {
    const res = await fetch(`${RAW}/config.yml`, {
      headers: { "user-agent": "bgnotify-commands-worker" },
      cf: { cacheTtl: 300 },
    });
    if (!res.ok) return {};
    const text = await res.text();

    const labels = {};
    let current = "";
    for (const line of text.split("\n")) {
      const name = line.match(/^\s*-\s*name:\s*(\S+)/);
      if (name) {
        current = name[1];
        continue;
      }
      const label = line.match(/^\s*label:\s*(\S+)/);
      if (label && current) {
        labels[current] = label[1];
        current = "";
      }
    }
    return labels;
  } catch {
    return {};
  }
}
