"use strict";

// Página do link "keeper assume" que vai ao WhatsApp do dono quando ele assume o jogo em outro
// aparelho. O token só localiza a oferta; quem religa é a sessão Google do dono (o servidor
// confere). Sem sessão, o servidor já mandou ao Google e de volta para cá.

const TZ = "America/Sao_Paulo";
const $ = (id) => document.getElementById(id);
const token = window.location.pathname.split("/")[2] || "";
let csrf = "";
let busy = false;

const hhmm = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });

const REFUSED = {
  oferta_expirada:
    "Este link expirou ou já foi usado. O keeper continua desligado; para ele voltar, ligue pelo painel.",
  outra_conta:
    "Este link é do dono da instância, e você entrou com outra conta Google. Saia e entre com a conta dona da instância.",
};

function show(section) {
  for (const id of ["loading", "ask", "done", "refused"]) $(id).hidden = id !== section;
}

function refuse(code) {
  show("refused");
  $("refused-text").textContent = REFUSED[code] || "Não foi possível abrir o link.";
  $("switch").hidden = code !== "outra_conta";
}

async function load() {
  let res;
  try {
    res = await fetch(`/api/takeover/link/${encodeURIComponent(token)}`, {
      credentials: "same-origin", cache: "no-store",
    });
  } catch (err) {
    refuse("");
    return;
  }
  if (res.status === 401) {
    window.location.replace(`/auth/login?next=${encodeURIComponent(window.location.pathname)}`);
    return;
  }
  let data = {};
  try {
    data = await res.json();
  } catch (err) {
    // corpo sem JSON
  }
  csrf = data.csrf || "";
  if (data.email) $("who").textContent = data.email;
  if (!res.ok) {
    refuse(data.detail);
    return;
  }
  $("until").textContent = hhmm.format(new Date(data.expires_at));
  show("ask");
}

async function post(path, body) {
  if (busy) return null;
  busy = true;
  $("error").hidden = true;
  try {
    return await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "X-CSRF-Token": csrf, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    $("error").textContent = "Sem conexão com o servidor. Tente de novo.";
    $("error").hidden = false;
    return null;
  } finally {
    busy = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("retake").addEventListener("click", async () => {
    const res = await post("/api/takeover/retake", { token });
    if (!res) return;
    if (res.ok) {
      show("done");
      return;
    }
    let detail = "";
    try {
      detail = (await res.json()).detail;
    } catch (err) {
      // corpo sem JSON
    }
    refuse(detail);
  });
  $("switch").addEventListener("click", async () => {
    await post("/auth/logout");
    window.location.replace(`/auth/login?next=${encodeURIComponent(window.location.pathname)}`);
  });
  load();
});
