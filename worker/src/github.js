/**
 * Authentifizierte Zugriffe auf das Repo (GitHub-API).
 *
 * Bewusst getrennt von `repo.js`: Das liest nur öffentliche Dateien und
 * braucht kein Token. Hier geht es um Dinge, die etwas auslösen — und für die
 * gilt: **Token so eng wie möglich schneiden.**
 *
 * `GITHUB_TOKEN` sollte ein fein-granulares Token sein mit genau einem Recht,
 * `Actions: read and write`, auf genau dieses eine Repo. Anders als beim Gist
 * (dort können fein-granulare Token gar nichts) ist das hier die richtige Wahl:
 * Der Worker hängt an einer öffentlichen URL, was hier lecken kann, soll so
 * wenig können wie möglich. Ein klassisches Token mit `repo` könnte dagegen
 * Code ändern.
 */

const API = "https://api.github.com";

// Kontoname nach der Umbenennung von `prayGG`. GitHub leitet den alten Namen
// zwar weiter, aber nur zuverlässig bei GET: Ein 301 auf ein POST macht die
// Fetch-Spezifikation zu einem GET ohne Body — der Workflow-Dispatch käme also
// nie an. Deshalb hier der aktuelle Name, nicht der weitergeleitete.
export const REPO = "praygoated/bgnotify";
const WORKFLOW = "main.yml";
const BRANCH = "main";

export function githubConfigured(env) {
  return Boolean(env.GITHUB_TOKEN);
}

export const actionsUrl = () => `https://github.com/${REPO}/actions/workflows/${WORKFLOW}`;

/**
 * Einen Bot-Lauf anstoßen (`workflow_dispatch`).
 *
 * GitHub antwortet mit 204 und ohne Inhalt — es gibt also keine Lauf-ID, die
 * man zurückmelden könnte. Wer sie will, müsste hinterher die Lauf-Liste
 * abfragen und raten, welcher der eigene war; das ist die Sekunde Wartezeit
 * nicht wert, zumal der Bot sein Ergebnis ohnehin in die Channels postet.
 */
export async function dispatchRun(env) {
  const res = await fetch(
    `${API}/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "user-agent": "bgnotify-commands-worker",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: BRANCH }),
    }
  );
  if (res.status === 204) return;

  const detail = (await res.text().catch(() => "")).slice(0, 200);
  if (res.status === 403 || res.status === 404) {
    // GitHub antwortet auf fehlende Rechte mit 404 statt 403, damit man nicht
    // durch Ausprobieren herausfinden kann, was existiert. Die beiden Fälle
    // sind von außen nicht unterscheidbar — also beide gleich erklären.
    throw new Error(
      `Kein Zugriff auf die Actions (HTTP ${res.status}). Hat \`GITHUB_TOKEN\` das Recht *Actions: read and write* auf ${REPO}?`
    );
  }
  throw new Error(`Lauf anstoßen fehlgeschlagen (HTTP ${res.status}) ${detail}`);
}
