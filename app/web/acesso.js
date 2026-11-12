"use strict";

// Página do link que vai ao WhatsApp do dono da instância. O token só localiza o pedido; quem
// libera é a sessão Google do dono (o servidor confere). Sem sessão, o servidor já mandou ao
// Google e de volta para cá.

const TZ = "America/Sao_Paulo";
const $ = (id) => document.getElementById(id);
const token = window.location.pathname.split("/")[2] || "";
let csrf = "";
let requestId = "";
let busy = false;
let durationKey = "";

const fmt = new Intl.DateTimeFormat("pt-BR", {
  timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
});

function fmtTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : fmt.format(d);
}

const DURATIONS = {
  "1h": "por 1 hora",
  "4h": "por 4 horas",
  "12h": "por 12 horas",
  sempre: "sem prazo (sempre aberto)",
};

const REFUSED = {
  pedido_nao_encontrado: "Esse pedido expirou ou já foi respondido.",
  outra_conta:
    "Esse pedido é para o dono da instância, e você entrou com outra conta Google. Saia e entre com a conta dona da instância.",
};

function show(section) {
  for (const id of ["loading", "ask", "done", "refused"]) $(id).hidden = id !== section;
}

function showError(message) {
  $("error").textContent = message || "";
  $("error").hidden = !message;
}

async function load() {
  let res;
  try {
    res = await fetch(`/api/access/link/${encodeURIComponent(token)}`, {
      credentials: "same-origin", cache: "no-store",
    });
  } catch (err) {
    show("refused");
    $("refused-text").textContent = "Sem conexão com o servidor. Tente de novo.";
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
    show("refused");
    $("refused-text").textContent = REFUSED[data.detail] || "Não foi possível abrir o pedido.";
    $("switch").hidden = data.detail !== "outra_conta";
    return;
  }
  const req = data.request;
  requestId = req.id;
  $("requester").textContent = req.requester;
  $("hunter").textContent = req.hunter ? ` (${req.hunter})` : "";
  $("duration").textContent = DURATIONS[req.duration] || req.duration;
  $("reason").textContent = `Motivo: ${req.reason || "—"}`;
  $("times").textContent =
    `Pedido às ${fmtTime(req.created_at)} · convite vale até ${fmtTime(req.expires_at)}`;
  durationKey = req.duration;
  show("ask");
}

async function post(path, body) {
  if (busy) return null;
  busy = true;
  showError("");
  try {
    return await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "X-CSRF-Token": csrf, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    showError("Sem conexão com o servidor. Tente de novo.");
    return null;
  } finally {
    busy = false;
  }
}

async function decide(path, body, doneText) {
  const res = await post(path, body);
  if (!res) return;
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json()).detail;
    } catch (err) {
      // corpo sem JSON
    }
    showError(REFUSED[detail] || "Não foi possível. Atualize a página e tente de novo.");
    return;
  }
  $("done-text").textContent = doneText;
  show("done");
}

document.addEventListener("DOMContentLoaded", () => {
  $("approve").addEventListener("click", () => {
    const label = DURATIONS[durationKey] || "";
    decide(
      `/api/access/requests/${encodeURIComponent(requestId)}/approve`,
      null,
      `Acesso aceito ${label}, contando a partir de agora. Dá para fechar a qualquer momento no painel, em "Acesso à sua instância".`,
    );
  });
  $("deny").addEventListener("click", () => {
    decide(
      `/api/access/requests/${encodeURIComponent(requestId)}/deny`,
      null,
      "Pedido recusado. A pessoa só consegue pedir de novo quando este pedido expirar.",
    );
  });
  $("switch").addEventListener("click", async () => {
    await post("/auth/logout");
    window.location.replace(`/auth/login?next=${encodeURIComponent(window.location.pathname)}`);
  });
  load();
});
