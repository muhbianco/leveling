"use strict";

// Painel do Leveling Keeper. Sem build: um arquivo, fetch na própria origem.
// Horários chegam em ISO com "Z" e são mostrados no fuso de São Paulo.

const TZ = "America/Sao_Paulo";
const STATUS_EVERY_MS = 5000;
const SHOT_EVERY_MS = 30000;

const PHASES = {
  desligado: ["Desligado", "off"],
  abrindo: ["Abrindo o jogo", "busy"],
  entrando: ["Entrando", "busy"],
  jogando: ["Jogando", "ok"],
  reconectando: ["Reconectando", "warn"],
  manutencao: ["Jogo em manutenção", "warn"],
  precisa_login: ["Precisa de login", "bad"],
  assumido: ["Assumido por outro aparelho", "off"],
  erro: ["Desligado por erro", "bad"],
};

const $ = (id) => document.getElementById(id);
let csrf = "";
let current = null;
let lastShot = 0;
let busy = false;

const timeFmt = new Intl.DateTimeFormat("pt-BR", {
  timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
});

function fmtTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : timeFmt.format(d);
}

function fmtSeconds(s) {
  if (s === null || s === undefined) return "—";
  if (s < 60) return `há ${Math.round(s)} s`;
  if (s < 3600) return `há ${Math.round(s / 60)} min`;
  return `há ${(s / 3600).toFixed(1)} h`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.method && options.method !== "GET") {
    headers["X-CSRF-Token"] = csrf;
    if (options.body) headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  if (res.status === 401) {
    window.location.replace("/");
    throw new Error("login");
  }
  return res;
}

function render(s) {
  current = s;
  csrf = s.csrf;
  const [label, tone] = PHASES[s.phase] || [s.phase, "warn"];
  const toggle = $("toggle");
  toggle.setAttribute("aria-pressed", String(s.desired_on));
  toggle.classList.toggle("on", s.desired_on);
  $("toggle-label").textContent = s.desired_on ? "Ligado no servidor" : "Desligado";
  const badge = $("phase");
  badge.textContent = label;
  badge.className = `badge ${tone}`;
  $("phase-since").textContent = s.phase_since ? `desde ${fmtTime(s.phase_since)}` : "";
  $("reason").textContent = s.desired_on ? s.reason || "" : s.off_detail || "";
  $("signal").textContent = s.desired_on ? fmtSeconds(s.signal_age) : "—";
  $("reconnects").textContent = String(s.reconnects_24h);
  $("retry").textContent = s.retry_in ? `em ${Math.ceil(s.retry_in)} s` : "—";
  $("viewers").textContent = s.remote_viewers ? `${s.remote_viewers} conectada(s)` : "fechada";
  $("vnc").href = s.vnc_url;
  $("reload").disabled = !s.desired_on;
  $("retry-login").hidden = !(s.login_latched || (s.phase === "precisa_login" && s.has_credentials));
}

async function refreshStatus() {
  try {
    const res = await api("/api/status");
    if (res.ok) render(await res.json());
  } catch (err) {
    // Rede caiu ou sessão expirou: o próximo ciclo tenta de novo.
  }
}

async function refreshEvents() {
  try {
    const res = await api("/api/events?limit=50");
    if (!res.ok) return;
    const { events } = await res.json();
    const list = $("events");
    list.replaceChildren(
      ...events.map((e) => {
        const li = document.createElement("li");
        li.className = `ev ${e.kind}`;
        const when = document.createElement("time");
        when.dateTime = e.at;
        when.textContent = fmtTime(e.at);
        const text = document.createElement("span");
        text.textContent = e.message;
        li.append(when, text);
        return li;
      }),
    );
  } catch (err) {
    // idem
  }
}

async function refreshShot(force = false) {
  const empty = $("shot-empty");
  if (!current || !current.desired_on) {
    $("shot").hidden = true;
    empty.textContent = "Sem imagem: o navegador do servidor está fechado.";
    empty.hidden = false;
    return;
  }
  if ($("shot").hidden) empty.textContent = "Carregando a tela do servidor…";
  if (!force && Date.now() - lastShot < SHOT_EVERY_MS) return;
  lastShot = Date.now();
  try {
    const res = await api("/api/screenshot");
    if (!res.ok) return;
    const blob = await res.blob();
    const img = $("shot");
    const old = img.src;
    img.src = URL.createObjectURL(blob);
    if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    img.hidden = false;
    $("shot-empty").hidden = true;
  } catch (err) {
    // idem
  }
}

async function post(path, body) {
  if (busy) return null;
  busy = true;
  try {
    return await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
  } finally {
    busy = false;
  }
}

async function tick(force = false) {
  if (document.hidden && !force) return;
  await refreshStatus();
  await refreshEvents();
  await refreshShot(force);
}

document.addEventListener("DOMContentLoaded", () => {
  $("toggle").addEventListener("click", async () => {
    if (!current) return;
    const res = await post("/api/power", { on: !current.desired_on });
    if (res && res.ok) render(await res.json());
    await refreshEvents();
  });
  $("reload").addEventListener("click", async () => {
    await post("/api/reload");
    setTimeout(() => tick(true), 3000);
  });
  $("retry-login").addEventListener("click", async () => {
    await post("/api/retry-login");
    await tick(true);
  });
  $("logout").addEventListener("click", async () => {
    await post("/auth/logout");
    window.location.replace("/");
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) tick(true);
  });
  tick(true);
  setInterval(() => tick(false), STATUS_EVERY_MS);
});
