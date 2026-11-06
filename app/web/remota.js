// Tela remota do Leveling Keeper sobre o núcleo do noVNC (core/rfb.js, servido em /vnc/ só para
// quem tem sessão). A UI padrão do noVNC continua em /vnc/vnc.html como reserva.
//
// O que esta página acrescenta ao vnc.html: barra compacta, rolagem sem roda (teclas Page Up/Down,
// Home/End), "segurar" o botão do mouse para arrastar sem manter pressionado, Esc, qualidade
// nítida/rápida, ajustar/1:1, tela cheia e reconexão com estado visível.
//
// Transporte: o servidor (X0tigervnc) manda quadros sem esperar pedido (ContinuousUpdates), então
// a taxa não depende da ida e volta até a hel1; a latência de cada ação é ~metade do ping.

import RFB from "/vnc/core/rfb.js";
import KeyTable from "/vnc/core/input/keysym.js";

const $ = (id) => document.getElementById(id);
const QUALITY_KEY = "lk.remota.quality";
const FIT_KEY = "lk.remota.fit";
// Qualidade = JPEG das áreas com textura (o jogo); o zlib fica em 2 (abaixar aumentou os bytes
// no TigerVNC, docs/tela-remota.md).
const PRESETS = { nitida: { quality: 6, compression: 2 }, rapida: { quality: 2, compression: 2 } };
const RECONNECT_START_MS = 2000;
const RECONNECT_MAX_MS = 30_000;
const REPEAT_FIRST_MS = 400;
const REPEAT_MS = 180;

const wsUrl = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/vnc/ws`;
let rfb = null;
let wantConnected = true;
let reconnectDelay = RECONNECT_START_MS;
let reconnectTimer = null;
let countdownTimer = null;
let holdState = "off"; // off | armed | holding

// --- estado na barra ----------------------------------------------------------------------------

function setState(text, tone) {
  const el = $("state");
  el.textContent = text;
  el.dataset.tone = tone;
  document.title = `${text} · Tela remota`;
}

function showOverlay(text) {
  $("overlay-text").textContent = text;
  $("overlay").hidden = false;
}

function hideOverlay() {
  $("overlay").hidden = true;
}

// --- conexão -----------------------------------------------------------------------------------

function preset() {
  const name = localStorage.getItem(QUALITY_KEY) || "nitida";
  return PRESETS[name] ? name : "nitida";
}

function applyPreset(name) {
  const p = PRESETS[name];
  if (!rfb || !p) return;
  rfb.qualityLevel = p.quality;
  rfb.compressionLevel = p.compression;
}

function connect() {
  clearTimeout(reconnectTimer);
  clearInterval(countdownTimer);
  reconnectTimer = null;
  setState("conectando…", "busy");
  rfb = new RFB($("screen"), wsUrl, { shared: true });
  rfb.scaleViewport = $("fit").getAttribute("aria-pressed") === "true";
  rfb.clipViewport = false;
  rfb.resizeSession = false;
  rfb.showDotCursor = false;
  applyPreset(preset());
  rfb.addEventListener("connect", onConnect);
  rfb.addEventListener("disconnect", onDisconnect);
  rfb.addEventListener("securityfailure", (e) => {
    wantConnected = false;
    showOverlay(`O servidor recusou a conexão (${e.detail.reason || e.detail.status}).`);
  });
  rfb.addEventListener("desktopname", () => {});
  rfb.addEventListener("clipboard", () => {});
}

function onConnect() {
  reconnectDelay = RECONNECT_START_MS;
  hideOverlay();
  setState(`conectado · ${rfb._fbWidth}×${rfb._fbHeight}`, "ok");
  bindCanvas();
  rfb.focus();
}

function onDisconnect(e) {
  setHold("off");
  if (!wantConnected) {
    setState("desconectado", "bad");
    return;
  }
  const why = e.detail.clean ? "A conexão fechou." : "A conexão caiu.";
  setState("reconectando…", "warn");
  scheduleReconnect(why);
}

function scheduleReconnect(why) {
  let left = Math.round(reconnectDelay / 1000);
  const tick = () => showOverlay(`${why} Reconectando em ${left} s…`);
  tick();
  clearInterval(countdownTimer);
  countdownTimer = setInterval(() => {
    left = Math.max(0, left - 1);
    tick();
  }, 1000);
  reconnectTimer = setTimeout(() => {
    reconnectDelay = Math.min(RECONNECT_MAX_MS, reconnectDelay * 2);
    connect();
  }, reconnectDelay);
}

// --- teclas e rolagem ---------------------------------------------------------------------------

function tap(keysym, code) {
  if (!rfb) return;
  rfb.sendKey(keysym, code, true);
  rfb.sendKey(keysym, code, false);
}

// Clique manda uma vez; segurar o botão repete (útil no celular e em listas longas).
function repeatOnHold(button, action) {
  let first = null;
  let repeat = null;
  const stop = () => {
    clearTimeout(first);
    clearInterval(repeat);
    first = repeat = null;
    if (rfb) rfb.focus();
  };
  button.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    action();
    first = setTimeout(() => {
      repeat = setInterval(action, REPEAT_MS);
    }, REPEAT_FIRST_MS);
  });
  for (const type of ["pointerup", "pointerleave", "pointercancel"]) button.addEventListener(type, stop);
}

// --- segurar o botão (arrastar sem manter pressionado) -------------------------------------------
//
// Usa dois campos internos do RFB do noVNC 1.3.0 (_mouseButtonMask e _sendMouse): "armado" espera
// o próximo clique no que se quer arrastar; quando o noVNC soltar o botão, pressiona de novo na
// mesma posição e deixa pressionado. Os movimentos seguintes vão com o botão apertado; o próximo
// clique solta (o noVNC zera a máscara no mouseup) e o estado volta a "off".

function setHold(state) {
  holdState = state;
  const btn = $("drag-lock");
  btn.setAttribute("aria-pressed", String(state !== "off"));
  btn.textContent = state === "holding" ? "Soltar" : state === "armed" ? "Clique no alvo…" : "Segurar";
  document.body.classList.toggle("holding", state === "holding");
}

function canHold() {
  return rfb && typeof rfb._sendMouse === "function" && "_mouseButtonMask" in rfb;
}

function onCanvasMouseUp() {
  if (!canHold()) return;
  if (holdState === "armed") {
    const pos = rfb._mousePos || { x: 0, y: 0 };
    rfb._mouseButtonMask |= 1;
    rfb._sendMouse(pos.x, pos.y, rfb._mouseButtonMask);
    setHold("holding");
  } else if (holdState === "holding" && (rfb._mouseButtonMask & 1) === 0) {
    setHold("off");
  }
}

function releaseHold() {
  if (holdState === "holding" && canHold()) {
    const pos = rfb._mousePos || { x: 0, y: 0 };
    rfb._mouseButtonMask &= ~1;
    rfb._sendMouse(pos.x, pos.y, rfb._mouseButtonMask);
  }
  setHold("off");
}

let boundCanvas = null;
function bindCanvas() {
  const canvas = rfb && rfb._canvas;
  if (!canvas || canvas === boundCanvas) return;
  boundCanvas = canvas;
  // Depois do próprio noVNC (fase de borbulha): aqui a máscara já reflete o clique.
  canvas.addEventListener("mouseup", onCanvasMouseUp);
}

// --- barra ----------------------------------------------------------------------------------------

function setFit(on) {
  $("fit").setAttribute("aria-pressed", String(on));
  $("fit").textContent = on ? "Ajustar" : "1:1";
  localStorage.setItem(FIT_KEY, on ? "1" : "0");
  if (rfb) rfb.scaleViewport = on;
}

function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen();
  } else {
    document.documentElement.requestFullscreen().catch(() => {});
  }
}

function showBar(show) {
  $("bar").hidden = !show;
  $("show-bar").hidden = show;
  document.documentElement.style.setProperty("--bar-h", show ? "32px" : "0px");
  // O noVNC recalcula a escala quando o espaço muda.
  window.dispatchEvent(new Event("resize"));
}

document.addEventListener("DOMContentLoaded", () => {
  $("quality").value = preset();
  $("quality").addEventListener("change", () => {
    localStorage.setItem(QUALITY_KEY, $("quality").value);
    applyPreset($("quality").value);
    if (rfb) rfb.focus();
  });
  setFit(localStorage.getItem(FIT_KEY) !== "0");
  $("fit").addEventListener("click", () => setFit($("fit").getAttribute("aria-pressed") !== "true"));
  $("fullscreen").addEventListener("click", toggleFullscreen);
  document.addEventListener("fullscreenchange", () => {
    $("fullscreen").textContent = document.fullscreenElement ? "Sair da tela cheia" : "Tela cheia";
  });
  $("hide").addEventListener("click", () => showBar(false));
  $("show-bar").addEventListener("click", () => showBar(true));

  repeatOnHold($("scroll-up"), () => tap(KeyTable.XK_Page_Up, "PageUp"));
  repeatOnHold($("scroll-down"), () => tap(KeyTable.XK_Page_Down, "PageDown"));
  $("scroll-home").addEventListener("click", () => { tap(KeyTable.XK_Home, "Home"); rfb && rfb.focus(); });
  $("scroll-end").addEventListener("click", () => { tap(KeyTable.XK_End, "End"); rfb && rfb.focus(); });
  $("esc").addEventListener("click", () => { tap(KeyTable.XK_Escape, "Escape"); rfb && rfb.focus(); });
  $("drag-lock").addEventListener("click", () => {
    if (!canHold()) return;
    if (holdState === "off") setHold("armed");
    else releaseHold();
    rfb.focus();
  });
  $("reconnect").addEventListener("click", () => {
    wantConnected = true;
    reconnectDelay = RECONNECT_START_MS;
    if (rfb) {
      // Um RFB desconectado não reconecta: cria outro.
      rfb = null;
    }
    connect();
  });

  connect();
});

window.addEventListener("beforeunload", () => {
  wantConnected = false;
  if (rfb) rfb.disconnect();
});
