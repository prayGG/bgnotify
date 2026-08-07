/**
 * Zugangsdaten als GitHub-Secret ablegen — verschlüsselt, ohne Umweg.
 *
 * WARUM ÜBERHAUPT SECRETS UND NICHT DAS GIST: Das Repo ist öffentlich, und
 * damit sind es auch die Action-Logs. GitHub maskiert Secrets darin
 * automatisch; ein Passwort aus dem Gist könnte über einen Stacktrace im
 * Klartext in einem öffentlich lesbaren Log landen.
 *
 * GitHub nimmt Secrets ausschließlich als **libsodium sealed box** an: Man holt
 * den öffentlichen Schlüssel des Repos, verschlüsselt damit, und selbst GitHub
 * kann den Wert danach nur noch in Workflows entschlüsseln. Der Worker sieht
 * das Passwort also genau einmal — im Arbeitsspeicher, für die Dauer eines
 * Requests.
 *
 * > Ehrliche Grenze: Der Bot muss sich damit bei BG einloggen, braucht sie also
 * > im Klartext. Wer Code und Secrets kontrolliert, kann sie sich jederzeit
 * > ausgeben lassen. Das Modal verlagert Vertrauen, es macht es nicht
 * > überflüssig. Nicht als Unantastbarkeit verkaufen.
 */

import nacl from "tweetnacl";
import sealedbox from "tweetnacl-sealedbox-js";

import { REPO } from "./github.js";

const API = "https://api.github.com";

function headers(env) {
  return {
    authorization: `Bearer ${env.GITHUB_TOKEN}`,
    accept: "application/vnd.github+json",
    "user-agent": "bgnotify-commands-worker",
    "content-type": "application/json",
  };
}

const b64encode = (bytes) => btoa(String.fromCharCode(...bytes));
const b64decode = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

/** Öffentlicher Schlüssel des Repos — wechselt selten, aber nie zwischenspeichern:
 *  Ein veralteter Schlüssel führt zu Secrets, die niemand mehr entschlüsseln kann. */
async function repoPublicKey(env) {
  const res = await fetch(`${API}/repos/${REPO}/actions/secrets/public-key`, {
    headers: headers(env),
  });
  if (!res.ok) {
    throw new Error(
      `Schlüssel des Repos nicht abrufbar (HTTP ${res.status}). Hat \`GITHUB_TOKEN\` das Recht *Secrets: read and write*?`
    );
  }
  return res.json(); // { key, key_id }
}

/**
 * Ein Secret setzen. `wert` verlässt diese Funktion nur verschlüsselt.
 *
 * `nacl.setPRNG` ist NICHT nötig: tweetnacl nutzt von sich aus
 * `crypto.getRandomValues`, das die Workers-Runtime bereitstellt. Ohne echten
 * Zufall wäre der ephemere Schlüssel vorhersagbar und die Versiegelung wertlos —
 * deshalb hier die ausdrückliche Prüfung statt stillem Vertrauen.
 */
export async function putSecret(env, name, wert, publicKey = null) {
  if (typeof crypto?.getRandomValues !== "function") {
    throw new Error("Kein sicherer Zufall verfügbar — Verschlüsselung abgebrochen.");
  }
  const pk = publicKey || (await repoPublicKey(env));

  const versiegelt = sealedbox.seal(new TextEncoder().encode(wert), b64decode(pk.key));

  const res = await fetch(`${API}/repos/${REPO}/actions/secrets/${name}`, {
    method: "PUT",
    headers: headers(env),
    body: JSON.stringify({ encrypted_value: b64encode(versiegelt), key_id: pk.key_id }),
  });
  // 201 = neu angelegt, 204 = bestehendes überschrieben
  if (res.status !== 201 && res.status !== 204) {
    throw new Error(`Secret ${name} setzen fehlgeschlagen (HTTP ${res.status})`);
  }
  return pk;
}

/** Secret löschen. Ein fehlendes Secret ist kein Fehler — Ziel ist „weg". */
export async function deleteSecret(env, name) {
  const res = await fetch(`${API}/repos/${REPO}/actions/secrets/${name}`, {
    method: "DELETE",
    headers: headers(env),
  });
  if (res.status !== 204 && res.status !== 404) {
    throw new Error(`Secret ${name} löschen fehlgeschlagen (HTTP ${res.status})`);
  }
}

/** Mehrere Secrets mit EINEM Schlüsselabruf setzen. */
export async function putSecrets(env, paare) {
  let pk = null;
  for (const [name, wert] of Object.entries(paare)) {
    pk = await putSecret(env, name, wert, pk);
  }
}

export { nacl };
