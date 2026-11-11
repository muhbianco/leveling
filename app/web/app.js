"use strict";

// Painel do Leveling Keeper. Sem build: um arquivo, fetch na própria origem.
// Horários chegam em ISO com "Z" e são mostrados no fuso de São Paulo.
// Cada conta Google vê só a própria instância: o servidor escolhe o slot pela sessão.

const TZ = "America/Sao_Paulo";
const STATUS_EVERY_MS = 5000;
const SHOT_EVERY_MS = 30000;
const EVENTS_LIMIT = 100;
const EVENTS_OPEN_KEY = "lk.events.open";

const PHASES = {
  desligado: ["Desligado", "off"],
  abrindo: ["Abrindo o jogo", "busy"],
  entrando: ["Entrando", "busy"],
  jogando: ["Jogando", "ok"],
  reconectando: ["Reconectando", "warn"],
  manutencao: ["Jogo em manutenção", "warn"],
  precisa_login: ["Precisa de login", "bad"],
  assumido: ["Assumido por outro aparelho", "off"],
  confirmando: ["Esperando sua confirmação", "warn"],
  erro: ["Desligado por erro", "bad"],
};

const SOURCES = {
  painel: "cadastrada neste painel",
  servidor: "arquivo de segredos do servidor",
};

const ERRORS = {
  sem_vaga: "Sem vaga: o servidor já roda o máximo de instâncias.",
  sem_chave: "O servidor ainda não tem a chave para guardar senhas. Avise o dono.",
  sem_instancia: "Sua instância não existe mais. Atualize a página.",
  contas_demais: "Limite de contas cadastradas. Remova uma antes de adicionar outra.",
  conta_em_uso:
    "Essa conta já está ativa na outra instância deste servidor: o jogo derrubaria as duas. Troque a conta de lá antes.",
  conta_ativa: "Essa é a conta em uso. Troque para outra antes de remover.",
  conta_do_servidor: "Essa conta vem do arquivo de segredos do servidor e não sai pelo painel.",
  conta_nao_encontrada: "Conta não encontrada. Atualize a página.",
  troca_falhou: "Não deu para limpar o navegador do servidor. Tente trocar de novo.",
  nao_admin: "Só admin pede acesso à instância de outro jogador.",
  proprio_slot: "Essa instância é a sua.",
  slot_nao_encontrado: "Essa instância não está mais no ar. Atualize a página.",
  ja_liberado: "Você já tem acesso a essa instância.",
  pedido_pendente: "Já tem um pedido seu esperando o dono responder.",
  pedido_recusado: "O dono recusou o último pedido. Dá para pedir de novo quando ele expirar.",
  pedidos_demais: "Pedidos demais abertos. Espere alguns expirarem.",
  acessos_demais: "Acessos demais liberados. Revogue algum antes.",
  pedido_nao_encontrado: "Esse pedido expirou ou já foi respondido. Atualize a página.",
  acesso_nao_encontrado: "Esse acesso já acabou. Atualize a página.",
  outra_conta: "Esse pedido é para outra conta Google.",
};

// Estado do aviso de pedido de acesso (WhatsApp do dono da instância).
const NOTIFY = {
  enviando: "Enviando o pedido…",
  enviado: "Pedido enviado no WhatsApp do dono.",
  falhou: "O WhatsApp falhou. O pedido está no painel do dono; dá para pedir de novo.",
  sem_telefone: "O dono não tem WhatsApp cadastrado. O pedido está no painel dele.",
  sem_whatsapp: "O WhatsApp não está configurado no servidor. O pedido está no painel do dono.",
};

const GRANT_CHOICES = [
  [1, "Liberar 1 h"],
  [12, "12 h"],
  [168, "7 dias"],
];

const $ = (id) => document.getElementById(id);
let csrf = "";
let current = null;
let lastShot = 0;
let busy = false;
let eventsSig = "";
let accountsSig = "";
let sharedSig = "";
let myAccessSig = "";
let confirmUntil = 0;
let confirmTimer = null;
const BASE_TITLE = document.title;

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

function showError(id, message) {
  const el = $(id);
  el.textContent = message || "";
  el.hidden = !message;
}

function renderSetup(s) {
  const full = s.capacity.used >= s.capacity.max;
  const free = s.capacity.max - s.capacity.used;
  $("setup-capacity").textContent = full
    ? `Sem vaga: o servidor já roda ${s.capacity.max} instâncias.`
    : `Informe o caçador e a senha do jogo para criar sua instância (${free} de ${s.capacity.max} vagas livres).`;
  for (const el of $("setup-form").elements) el.disabled = full;
}

function renderPanel(s) {
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
  renderAccounts(s.slot);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function accountButton(label, action, account, extra = "") {
  const btn = el("button", `button small ${extra}`.trim(), label);
  btn.type = "button";
  btn.dataset.action = action;
  btn.dataset.id = account.id;
  btn.dataset.name = account.username;
  return btn;
}

function renderAccounts(slot) {
  // Redesenha só quando muda: o status chega a cada 5 s e não pode roubar foco nem clique.
  const sig = JSON.stringify([slot.accounts, slot.max_accounts]);
  if (sig === accountsSig) return;
  accountsSig = sig;
  const items = slot.accounts.map((a) => {
    const li = el("li", a.active ? "active" : "");
    li.append(el("span", "name", a.username), el("span", "origin", SOURCES[a.source] || a.source));
    const actions = el("span", "row-actions");
    if (a.active) {
      actions.append(el("span", "badge ok", "ativa"));
    } else {
      actions.append(accountButton("Usar esta", "activate", a));
      if (a.source === "painel") actions.append(accountButton("Remover", "remove", a, "danger"));
    }
    li.append(actions);
    return li;
  });
  if (!slot.accounts.some((a) => a.active)) {
    items.unshift(el("li", "empty", "Nenhuma conta ativa: escolha uma ou adicione abaixo."));
  }
  $("accounts").replaceChildren(...items);
  const full = slot.accounts.filter((a) => a.source === "painel").length >= slot.max_accounts;
  $("add-account").querySelector("summary").textContent = full
    ? `Trocar senha (limite de ${slot.max_accounts} contas)`
    : "Adicionar conta ou trocar senha";
}

function render(s) {
  current = s;
  csrf = s.csrf;
  $("who").textContent = s.access && s.access.admin ? `${s.email} · admin` : s.email;
  const hasSlot = Boolean(s.slot);
  $("setup").hidden = hasSlot;
  $("panel").hidden = !hasSlot;
  if (hasSlot) renderPanel(s);
  else renderSetup(s);
  renderShared(s.access);
  renderMyAccess(s.access);
  renderConfirm(s);
}

function personName(name, email) {
  return name || email.split("@")[0];
}

function accessButton(label, action, data, extra = "") {
  const btn = el("button", `button small ${extra}`.trim(), label);
  btn.type = "button";
  btn.dataset.accessAction = action;
  for (const [key, value] of Object.entries(data)) btn.dataset[key] = String(value);
  return btn;
}

// Instâncias dos outros (só admin): pedir, acompanhar o pedido e abrir a tela liberada.
function renderShared(access) {
  const box = $("shared");
  const list = access && access.shared;
  box.hidden = !list;
  if (!list) return;
  // Redesenha só quando muda: o status chega a cada 5 s e não pode roubar o clique.
  const sig = JSON.stringify(list);
  if (sig === sharedSig) return;
  sharedSig = sig;
  if (!list.length) {
    $("shared-list").replaceChildren(el("li", "empty", "Nenhuma outra instância no ar agora."));
    return;
  }
  $("shared-list").replaceChildren(
    ...list.map((item) => {
      const a = item.access;
      const li = el("li", a.state === "liberado" ? "active" : "");
      const [phase, tone] = PHASES[item.phase] || [item.phase, "warn"];
      li.append(
        el("span", "name", personName(item.name, item.email)),
        el("span", "origin", item.email),
        el("span", `badge ${tone}`, phase),
      );
      const actions = el("span", "row-actions");
      const note = el("p", "hint wide");
      if (a.state === "liberado") {
        const open = el("a", "button small primary", "Abrir tela remota");
        open.href = a.remote_url;
        open.target = "_blank";
        open.rel = "noopener";
        actions.append(open, accessButton("Sair", "leave", { id: a.grant_id }));
        note.textContent = `Liberado até ${fmtTime(a.expires_at)}.`;
        if (!item.desired_on) {
          note.textContent += " O keeper dele está desligado: a tela fica vazia até ele ligar.";
        }
      } else if (a.state === "pendente") {
        if (a.notify === "falhou") {
          actions.append(accessButton("Pedir de novo", "request", { slot: item.slot_id }));
        }
        actions.append(accessButton("Cancelar", "cancel", { id: a.request_id }));
        note.textContent = `${NOTIFY[a.notify] || "Pedido enviado."} Vale até ${fmtTime(a.expires_at)}.`;
      } else if (a.state === "recusado") {
        note.textContent = `O dono recusou. Novo pedido a partir de ${fmtTime(a.expires_at)}.`;
      } else {
        actions.append(accessButton("Pedir acesso", "request", { slot: item.slot_id }, "primary"));
      }
      li.append(actions);
      if (note.textContent) li.append(note);
      return li;
    }),
  );
}

// Pedidos que chegaram para a minha instância e quem tem acesso a ela.
function renderMyAccess(access) {
  const incoming = (access && access.incoming) || [];
  const granted = (access && access.granted) || [];
  $("my-access").hidden = !incoming.length && !granted.length;
  const sig = JSON.stringify([incoming, granted]);
  if (sig === myAccessSig) return;
  myAccessSig = sig;
  $("incoming").replaceChildren(
    ...incoming.map((r) => {
      const li = el("li", "pending");
      li.append(
        el("span", "name", `${personName(r.name, r.email)} pede acesso`),
        el("span", "origin", `${r.email} · vale até ${fmtTime(r.expires_at)}`),
      );
      const actions = el("span", "row-actions");
      for (const [hours, label] of GRANT_CHOICES) {
        actions.append(
          accessButton(label, "approve", { id: r.id, hours }, hours === 1 ? "primary" : ""),
        );
      }
      actions.append(accessButton("Recusar", "deny", { id: r.id }, "danger"));
      li.append(actions);
      return li;
    }),
  );
  $("granted").replaceChildren(
    ...granted.map((g) => {
      const li = el("li", "active");
      li.append(
        el("span", "name", personName(g.name, g.email)),
        el("span", "origin", `${g.email} · liberado até ${fmtTime(g.expires_at)}`),
      );
      const actions = el("span", "row-actions");
      actions.append(accessButton("Revogar", "revoke", { id: g.id }, "danger"));
      li.append(actions);
      return li;
    }),
  );
}

async function onAccessAction(event) {
  const btn = event.target.closest("button[data-access-action]");
  if (!btn) return;
  const errorId = btn.closest("#shared") ? "shared-error" : "my-access-error";
  showError(errorId, "");
  const { accessAction: action, id, slot, hours } = btn.dataset;
  const path = `/api/access/requests/${encodeURIComponent(id || "")}`;
  let res;
  if (action === "request") {
    res = await send("/api/access/requests", "POST", { slot: Number(slot) });
  } else if (action === "cancel") {
    res = await send(path, "DELETE");
  } else if (action === "approve") {
    res = await send(`${path}/approve`, "POST", { hours: Number(hours) });
  } else if (action === "deny") {
    res = await send(`${path}/deny`, "POST");
  } else if (action === "revoke" || action === "leave") {
    const ask = action === "revoke"
      ? "Revogar o acesso? A tela remota de quem recebeu cai na hora."
      : "Abrir mão do acesso a essa instância?";
    if (!window.confirm(ask)) return;
    res = await send(`/api/access/grants/${encodeURIComponent(id)}`, "DELETE");
  } else {
    return;
  }
  if (!res) return;
  if (!res.ok) {
    showError(errorId, await errorFrom(res, "Não foi possível. Tente de novo."));
    await refreshStatus();
    return;
  }
  render(await res.json());
}

// A sessão caiu depois de jogar: o servidor segura o login por alguns segundos à espera do
// dono. A contagem é local (a partir do confirm_in do último status); o título da aba avisa
// mesmo com o painel em segundo plano.
function renderConfirm(s) {
  const modal = $("confirm-modal");
  const open = Boolean(s.slot) && s.desired_on && typeof s.confirm_in === "number";
  const wasOpen = !modal.hidden;
  if (open) {
    confirmUntil = Date.now() + s.confirm_in * 1000;
    if (!confirmTimer) confirmTimer = setInterval(updateCountdown, 250);
    updateCountdown();
  } else {
    confirmUntil = 0;
    if (confirmTimer) clearInterval(confirmTimer);
    confirmTimer = null;
    document.title = BASE_TITLE;
  }
  modal.hidden = !open;
  if (open && !wasOpen) $("confirm-elsewhere").focus();
}

function updateCountdown() {
  const left = Math.max(0, Math.ceil((confirmUntil - Date.now()) / 1000));
  $("confirm-count").textContent = String(left);
  document.title = left > 0 ? `(${left} s) Confirme no keeper` : "Keeper reconectando…";
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
  if (!current || !current.slot) return;
  try {
    const res = await api(`/api/events?limit=${EVENTS_LIMIT}`);
    if (!res.ok) return;
    const { events } = await res.json();
    // Só redesenha quando muda: não mexe na rolagem de quem está lendo o log.
    const sig = events.map((e) => `${e.at}|${e.kind}|${e.message}`).join("\n");
    if (sig === eventsSig) return;
    eventsSig = sig;
    const last = events[0];
    $("events-last").textContent = last ? `${fmtTime(last.at)} · ${last.message}` : "";
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
  if (!current || !current.slot || !current.desired_on) {
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
    empty.hidden = true;
  } catch (err) {
    // idem
  }
}

async function send(path, method, body) {
  if (busy) return null;
  busy = true;
  try {
    return await api(path, { method, body: body ? JSON.stringify(body) : undefined });
  } finally {
    busy = false;
  }
}

async function errorFrom(res, fallback = "Não foi possível salvar. Tente de novo.") {
  try {
    const data = await res.json();
    if (typeof data.detail === "string" && ERRORS[data.detail]) return ERRORS[data.detail];
  } catch (err) {
    // corpo sem JSON
  }
  if (res.status === 422) return "Confira o nome de caçador e a senha.";
  return fallback;
}

function bindCredentialsForm(formId, errorId) {
  const form = $(formId);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    showError(errorId, "");
    const data = new FormData(form);
    const res = await send("/api/accounts", "POST", {
      username: String(data.get("username") || ""),
      password: String(data.get("password") || ""),
    });
    if (!res) return;
    if (!res.ok) {
      showError(errorId, await errorFrom(res));
      return;
    }
    form.reset();
    const details = form.closest("details");
    if (details) details.open = false;
    render(await res.json());
    await tick(true);
  });
}

async function onAccountAction(event) {
  const btn = event.target.closest("button[data-action]");
  if (!btn || !current || !current.slot) return;
  showError("account-error", "");
  const { action, id, name } = btn.dataset;
  const path = `/api/accounts/${encodeURIComponent(id)}`;
  let res;
  if (action === "activate") {
    if (
      current.desired_on &&
      !window.confirm(`Trocar para ${name}? O navegador do servidor fecha e entra com ${name}.`)
    ) {
      return;
    }
    res = await send(`${path}/activate`, "POST");
  } else if (action === "remove") {
    if (!window.confirm(`Remover a conta ${name} do painel? A senha cadastrada é apagada.`)) return;
    res = await send(path, "DELETE");
  } else {
    return;
  }
  if (!res) return;
  if (!res.ok) {
    showError("account-error", await errorFrom(res, "Não foi possível. Tente de novo."));
    return;
  }
  render(await res.json());
  await tick(true);
}

function bindEventsBox() {
  const box = $("events-box");
  try {
    box.open = window.localStorage.getItem(EVENTS_OPEN_KEY) === "1";
  } catch (err) {
    // armazenamento bloqueado: começa fechado
  }
  box.addEventListener("toggle", () => {
    try {
      window.localStorage.setItem(EVENTS_OPEN_KEY, box.open ? "1" : "0");
    } catch (err) {
      // idem
    }
  });
}

async function tick(force = false) {
  // O status roda mesmo com a aba escondida (é leve): é ele que abre a confirmação.
  await refreshStatus();
  if (document.hidden && !force) return;
  await refreshEvents();
  await refreshShot(force);
}

document.addEventListener("DOMContentLoaded", () => {
  $("toggle").addEventListener("click", async () => {
    if (!current || !current.slot) return;
    const res = await send("/api/power", "POST", { on: !current.desired_on });
    if (res && res.ok) render(await res.json());
    await refreshEvents();
  });
  $("reload").addEventListener("click", async () => {
    await send("/api/reload", "POST");
    setTimeout(() => tick(true), 3000);
  });
  $("retry-login").addEventListener("click", async () => {
    await send("/api/retry-login", "POST");
    await tick(true);
  });
  $("delete-slot").addEventListener("click", async () => {
    const ok = window.confirm(
      "Apagar sua instância? O navegador do servidor fecha, o cadastro do jogo é apagado e a vaga fica livre.",
    );
    if (!ok) return;
    const res = await send("/api/slot", "DELETE");
    if (res && res.ok) render(await res.json());
    else if (res) showError("account-error", "Não foi possível apagar. Tente de novo.");
  });
  $("logout").addEventListener("click", async () => {
    await send("/auth/logout", "POST");
    window.location.replace("/");
  });
  bindCredentialsForm("setup-form", "setup-error");
  bindCredentialsForm("creds-form", "account-error");
  $("accounts").addEventListener("click", onAccountAction);
  $("shared").addEventListener("click", onAccessAction);
  $("my-access").addEventListener("click", onAccessAction);
  $("confirm-elsewhere").addEventListener("click", async () => {
    const res = await send("/api/confirm/elsewhere", "POST");
    if (res && res.ok) render(await res.json());
    await refreshEvents();
  });
  $("confirm-reconnect").addEventListener("click", async () => {
    const res = await send("/api/confirm/reconnect", "POST");
    if (res && res.ok) render(await res.json());
    else await refreshStatus();
    await refreshEvents();
  });
  bindEventsBox();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) tick(true);
  });
  tick(true);
  setInterval(() => tick(false), STATUS_EVERY_MS);
});
