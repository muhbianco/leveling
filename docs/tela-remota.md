# Tela remota: auditoria de latência e plano (06/11/2026)

Objetivo: fazer a tela remota do painel (Chromium do slot visto pelo navegador do jogador) parecer
um navegador local. Este documento registra como o caminho funciona hoje, o que foi medido, onde
está o tempo e o plano por prioridade. Os números de laboratório saem de `bench.py` (cliente
headless Playwright no Windows) contra um container da imagem de produção com 1 núcleo; os de
produção, do container na hel1 em 06/11/2026.

## 1. Arquitetura atual (ponta a ponta)

1. **Chromium do slot** (Playwright, `app/game.py`) desenha a janela `1280x720` num **Xvfb**
   próprio (`:99`/`:100`, `app/display.py`). O WebGL do jogo roda em software (SwiftShader).
   Até 06/11 a barra do Chromium ocupava os ~88 px de cima (o jogo via `1280x632`); desde a
   mudança desta auditoria o Chromium abre em tela cheia (`--start-fullscreen`) e o jogo vê
   `1280x720` (medido: viewport de 719 px com a flag, 632 sem).
2. **x11vnc 0.9.16** (`-noxdamage -localhost -forever -shared -nopw -quiet`) lê o framebuffer do
   Xvfb por *polling* (32 passadas de linhas, `-wait 20 -defer 20` por padrão), codifica
   (Tight/JPEG, qualidade pedida pelo cliente) e fala RFB em `127.0.0.1:590N`.
3. **Ponte** `app/vnc.py`: WebSocket `/vnc/ws` (uvicorn + `websockets`, permessage-deflate ligado
   por padrão) ⇄ TCP do x11vnc, 64 KiB por leitura, só depois de conferir sessão Google + Origin.
   Entre o navegador e a ponte há o Traefik (TLS).
4. **noVNC 1.3.0** (pacote Debian, `/usr/share/novnc`): `vnc.html?resize=scale&autoconnect=1`
   aberto em outra aba pelo botão "Abrir tela remota". Mouse: `mousemove` limitado a 1 a cada 17 ms;
   roda vira botões 4/5 em passos; teclado via keysym. Pede cada quadro com
   `FramebufferUpdateRequest` depois de receber o anterior (pedido-resposta), porque o x11vnc não
   oferece *ContinuousUpdates*.
5. Painel (`app/web/app.js`): status a cada 5 s, miniatura (`/api/screenshot`, JPEG via CDP) a
   cada 30 s. Enquanto a tela remota está aberta o supervisor só observa (modo manual).

Caminho de um clique: navegador → noVNC (≤17 ms de coalescência) → WSS (RTT/2) → ponte → x11vnc →
XTest → Xvfb → Chromium (fila de entrada do renderer) → repaint → Xvfb → x11vnc nota a mudança
(polling) → codifica → ponte → WSS (RTT/2) → noVNC decodifica → canvas.

## 2. Diagnóstico (com prova)

| # | Causa | Evidência | Peso |
|---|---|---|---|
| 1 | **CPU do container no teto.** Limite `2.0` núcleos; o processo GPU do Chromium (SwiftShader) gasta **1,31 núcleo por slot jogando**; dois slots pedem ~2,6. | `docker stats` na hel1: 1.174.197 de 1.349.551 períodos estrangulados (**87 %**). `top` do container em 06/11: `chrome --type=gpu-process` 131 %, renderer 15 %. A sonda do DOM (um `page.evaluate` trivial) estourava 5 s. Host: 8 núcleos, load 1–4. | Alto: atrasa tudo (entrada no Chromium, x11vnc, ponte) e é o que o jogador sente como "clique atrasado". |
| 2 | **Pedido-resposta por quadro + 232 ms de RTT.** O x11vnc/libvncserver 0.9.14 não implementa *ContinuousUpdates*; o noVNC só pede o próximo quadro depois de receber o anterior. | `ping leveling.muhbianco.com.br` daqui: 232 ms. Laboratório com 115 ms de atraso: 13–25 quadros/s e clique 279–306 ms (x11vnc) contra 108–120 quadros/s e 171 ms (TigerVNC, que tem *ContinuousUpdates*). `strings` da libvncserver não tem "continuous". | Alto: em produção a taxa fica em ~4 quadros/s e cada clique custa ≥ RTT + servidor. |
| 3 | **x11vnc com `-noxdamage` e polling padrão.** Sem XDAMAGE ele varre a tela inteira; `-wait 20 -defer 20` somam dezenas de ms; o Xvfb gasta tanto quanto o x11vnc só para servir as leituras. | Laboratório, tela animada, 1 núcleo: clique p50 **162 ms**, x11vnc 27 % + Xvfb 22 %. Com XDAMAGE e `-wait 10 -defer 5`: **83–97 ms**, mas 35 % + 27 %. TigerVNC: **43 ms**, 14 % + 2 %. Tela parada: 78 ms → 51 ms só tirando o `-noxdamage`. | Médio-alto. |
| 4 | **Largura de banda com a tela mudando.** O jogo redesenha o canvas inteiro a 60 fps; cada quadro é 1280x720 recodificado. | 555–760 kB/s (x11vnc) e 1,3–1,8 MB/s (TigerVNC sem teto) na tela animada; página de login real: 120–330 kB/s. `tx_bytes` do container: 913 MB em ~1,5 dia. | Médio: sem teto de quadros, uma conexão fraca enfileira e a latência explode. |
| 5 | **Arrastar "trava"** (botão fica preso para a página). | Reproduzido sem o noVNC (eventos RFB crus): o x11vnc entregou todos os `down/up` (log `-debug_pointer`), mas o Chromium só entregou o `mouseup` à página em 5 de 6 arrastos; com `user-select: none` na página, 6 de 6. É o **drag-and-drop nativo do Chromium** ao arrastar texto selecionado: a página recebe `dragstart`, não `mousemove/mouseup`. | Médio: acontece também num navegador local; piora pela latência. Não é do VNC. |
| 6 | **Visor sem controles.** `vnc.html` padrão: nada para rolar sem roda, sem tela cheia à mão, sem qualidade, sem indicação de botão preso; a aba separada usa o mesmo transporte (não é otimização). | Inspeção. | Médio (usabilidade). |
| 7 | **permessage-deflate na ponte.** Recomprime JPEG já comprimido. | Medido: 7,3 % → 7,4 % de CPU do processo da ponte com e sem (não há ganho mensurável; o cliente provavelmente não negocia). | Nulo. Desligar é só higiene. |
| 8 | Movimento do mouse: 125 eventos/s viram 63 enviados (17 ms do noVNC) e 37–48 entregues (1 por quadro do Chromium). Roda e PageDown chegam inteiros. Nenhum clique perdido ou duplicado fora do caso 5. | Laboratório. | Baixo: já adequado. |

Latências separadas (laboratório, tela animada, 1 núcleo, sem atraso de rede):

| | x11vnc hoje | x11vnc ajustado | TigerVNC |
|---|---|---|---|
| Clique → pixel novo no cliente (p50/p90) | 162 / 186 ms | 83–97 / 99–130 ms | 43 / 53 ms |
| Quadros/s | 38–42 | 50–60 | 99–123 (sem teto) |
| Banda | 555–760 kB/s | 900 kB/s | 1,3–1,5 MB/s (sem teto); 640–820 kB/s com `FrameRate=30` |
| CPU servidor VNC + Xvfb | 27 + 22 % | 35 + 27 % | 14 + 2 % |

Com 115 ms de atraso de ida (RTT 230 em produção é ~2×):

| | x11vnc hoje | x11vnc ajustado | TigerVNC | TigerVNC `FrameRate=30` |
|---|---|---|---|---|
| Clique p50 | 306 ms | 279 ms | 171 ms | 200 ms |
| Quadros/s (animado) | 11–13 | 13–25 | 108–120 | 51–53 |
| Banda | 176–219 kB/s | 233–370 kB/s | 1,4–1,8 MB/s | 640–820 kB/s (474–713 com qualidade 4) |

Latência de entrada (ação → execução no Chromium) ≈ RTT/2 + ≤17 ms + fila do Chromium (que em
produção é dominada pela causa 1). Latência visual (execução → pixel) ≈ detecção + codificação +
RTT/2 + decodificação; com pedido-resposta soma mais um RTT por quadro.

## 3. Mapa de problemas

- **P0** CPU estrangulada (1); pedido-resposta sem *ContinuousUpdates* (2); x11vnc com polling
  cego (3).
- **P1** Banda sem teto quando a tela anima (4); visor sem rolagem/tela cheia/qualidade (6).
- **P2** Barra do Chromium dentro da imagem (88 px perdidos; cliques nela por engano) — feito a
  pedido do dono, Chromium em tela cheia; noVNC 1.3.0 (2022) do Debian; deflate inútil (7).
- **P3** Arrastar texto selecionado (5): limitação do navegador, documentar; WebRTC.

## 4. Oportunidades por camada

- **Recursos:** limite de CPU da stack `2.0 → 4.0` (host com 8 núcleos e load 1–4). Memória: 1,4 GB
  usados de 3 GB; manter.
- **Servidor VNC:** TigerVNC `X0tigervnc` (XDAMAGE + `ComparingUpdateTracker` + Tight/JPEG +
  *ContinuousUpdates* + `FrameRate`): 3–4× menos CPU e metade da latência do melhor x11vnc; taxa
  independente do RTT. Alternativa sem trocar binário: x11vnc com `-wait 10 -defer 5 -nowf -noscr
  -xkb` (sem `-noxdamage`): −45 % na latência, mas +25 % de CPU e sem *ContinuousUpdates*.
- **Teto de quadros:** `-FrameRate=30` corta a banda pela metade com +30 ms de latência; qualidade
  JPEG 4 corta mais 25 %.
- **Cliente:** visor próprio sobre `core/rfb.js` com barra compacta (rolagem por teclas e roda,
  tela cheia, escala/1:1, qualidade, trava de arrasto, estado da conexão e reconexão).
- **Ponte:** manter (CPU baixa); desligar o deflate por higiene.
- **Pixels:** 960x540 dá −25 % de CPU/banda e −20 % de latência, mas muda o layout do jogo; fica
  como opção futura por slot.

## 5. Comparação tecnológica

| Opção | Latência | Fluidez no RTT 232 | CPU | Banda | Complexidade | Risco |
|---|---|---|---|---|---|---|
| x11vnc atual | alta | ~4 q/s | média | média | — | — |
| x11vnc ajustado | média | ~4–8 q/s | média-alta | média | baixa | baixo |
| **TigerVNC X0tigervnc** | baixa | 30 q/s (teto) | baixa | média (com teto) | baixa (mesmo noVNC, mesma ponte) | baixo (pacote Debian estável) |
| CDP `Page.startScreencast` + `Input.dispatch*` | baixa | ack por quadro (poucos em voo) | média (JPEG no próprio Chromium) | média | média (troca o cliente inteiro) | médio: perde a tela fora da página (diálogos, barra) |
| WebRTC (neko/GStreamer, VP8) | muito baixa | alta | alta (codec em software) | baixa | alta (STUN/TURN, novo serviço) | alto |
| Controle por DOM (Playwright) em vez de pixels | — | — | — | — | média | viola "só fica conectado": o robô passaria a clicar no jogo |

Recomendação: TigerVNC agora; CDP só se a barra/diálogos do Chromium deixarem de importar; WebRTC
só com GPU ou outro servidor.

## 6. Plano de implementação

| Pri. | Item | Causa | Arquivos | Impacto esperado | Risco | Complexidade | Validação |
|---|---|---|---|---|---|---|---|
| P0 | Limite de CPU `4.0` | 1 | `infra/docker-stack.yml` | fim do estrangulamento (87 % → ~0) com 2 slots; entrada do Chromium sem fila | baixo (host tem folga) | trivial | `docker stats`: `throttled_periods` parado; sonda do DOM sem timeout |
| P0 | TigerVNC no lugar do x11vnc (`FrameRate=30`, `MaxProcessorUsage=100`, `localhost`, `SecurityTypes=None`, `AlwaysShared`) | 2, 3, 4 | `Dockerfile`, `app/display.py`, README | clique −50 % local; 30 q/s independentes do RTT; CPU do VNC −70 % | baixo; rollback = imagem anterior | baixa | bench antes/depois; smoke do container |
| P1 | Visor próprio `/remota` | 6 | `app/web/remota.html|.js|.css`, `app/api.py` | rolagem sem roda, tela cheia, qualidade, trava de arrasto, reconexão | baixo (o `vnc.html` continua disponível) | média | manual no painel + testes de rota |
| P2 | Desligar permessage-deflate | 7 | `Dockerfile` (CMD) | nenhum mensurável | nulo | trivial | — |
| P2 | noVNC 1.6 vendorado | — | `Dockerfile` | decodificação mais rápida; UI de reserva nova | baixo | baixa | bench |
| P2 | Resolução por slot (960x540) | 4 | `config.py`, `display.py`, painel | −25 % CPU/banda | médio (layout do jogo) | média | bench |
| P3 | WebRTC | 2 | novo serviço | latência mínima | alto | alta | — |

Dependências: P0-CPU é independente. TigerVNC antes do visor (o visor assume *ContinuousUpdates*).

## 7. Implementação (06/11/2026)

Aprovado pelo dono: CPU, TigerVNC e visor próprio ("cuidado com a máquina, outros serviços rodam
lá"). Feito:

- **`infra/docker-stack.yml`:** limite de CPU `2.0 → 3.5` (não 4.0: a hel1 roda as outras stacks;
  medido ~0,6 núcleo ocupado em média no host com 8). Reserva e memória inalteradas.
- **`Dockerfile`:** `tigervnc-scraping-server` no lugar de `x11vnc`; uvicorn com
  `--ws-per-message-deflate false` (sem ganho medido, só higiene). Imagem 1,32 GB (antes 1,33).
- **`app/display.py`:** `X0tigervnc -display :N -rfbport P -localhost=1 -SecurityTypes=None
  -AlwaysShared=1 -FrameRate=<VNC_FRAME_RATE> -MaxProcessorUsage=100`. `VNC_FRAME_RATE`
  (`app/config.py`, padrão 30, 5–60) é o teto de quadros por cliente.
- **Visor próprio `/remota`** (`app/web/remota.html|js|css`, rota em `app/api.py` com os mesmos
  cabeçalhos de segurança do painel; `/vnc/` redireciona para ele; a UI padrão do noVNC fica em
  `/vnc/padrao`). Barra de 32 px, escondível: estado da conexão, ▲▼ (Page Up/Down, repetem ao
  segurar), ⇈⇊ (Home/End), "Segurar" (pressiona o botão no próximo clique e solta no seguinte,
  para arrastar sem manter apertado), Esc, qualidade nítida/rápida (JPEG 6 ou 2), ajustar/1:1,
  tela cheia. Reconexão com backoff 2–30 s e contagem na tela; "Reconectar agora".
- **Chromium em tela cheia** (`--start-fullscreen` em `app/game.py`, pedido do dono depois do
  primeiro deploy): o jogo ocupa os 1280x720 inteiros; F11 na tela remota alterna.
- **Testes:** `tests/test_display.py` (flags do servidor VNC), `tests/test_game.py` (tela cheia) e
  `/remota`, `/vnc/`, `/vnc/padrao`, estáticos e `vnc_url` em `tests/test_api.py`. 135 testes.
- **Smoke da imagem real:** container sobe, `/healthz` 200, Xvfb + X0tigervnc de pé, VNC só em
  `127.0.0.1:5900`.
- **Visor verificado no navegador** contra o container do laboratório: conecta (1280×720,
  ajustado à janela), ▼ rola a lista remota (Page Down chega à página), "Segurar" pega o quadrado,
  move com o botão apertado e solta no clique seguinte (posição final certa), sem erro de CSP.

Não feito (fica para outra vez): noVNC 1.6 vendorado; resolução por slot; WebRTC.

## 8. Benchmark antes/depois

Laboratório (container com 1 núcleo, cliente headless, tela animada em 1280x720 a 60 fps =
pior caso do jogo). "Antes" = imagem de produção de 04/11 (x11vnc `-noxdamage`); "depois" =
imagem desta mudança com as flags de produção (`-FrameRate=30`).

| Métrica | Antes | Depois | Δ |
|---|---|---|---|
| Clique → pixel, sem atraso de rede (p50 / p90) | 162 / 186 ms | 100 / 106 ms | −38 % / −43 % |
| Clique → pixel, 115 ms de atraso de ida (p50 / p90) | 306 / 334 ms | 206 / 215 ms | −33 % / −36 % |
| Clique → pixel, tela parada, 115 ms de atraso (p50) | ~190 ms (78 + RTT) | 157 ms | −17 % |
| Quadros/s com 115 ms de atraso | 11–13 | 24–50 | 2–4× |
| CPU do servidor VNC + Xvfb (tela animada) | 27 % + 22 % | 7,5 % + 1,5 % | −82 % |
| Banda (tela animada, sem atraso) | 555–760 kB/s | 715–850 kB/s | +15 % (mais quadros) |
| Banda (tela animada, 115 ms) | 176–219 kB/s | 293–700 kB/s | mais quadros |
| Movimentos: enviados → entregues à página | 63 → 37–48 | 63 → 71 | — |
| Cliques perdidos / duplicados | 0 / 0 | 0 / 0 | — |
| Arrastar (40 passos): entregues, posição final | 19–24, certa | 23–27, certa | — |
| Roda e Page Down | ok | ok | — |

Sem o teto de quadros (`VNC_FRAME_RATE=60`): 43 ms de clique sem atraso e 171 ms com 115 ms,
mas 1,3–1,8 MB/s na tela animada. O teto de 30 custa ~35–55 ms e corta a banda pela metade.

Qualidade "rápida" (JPEG 2): só faz diferença onde há textura (o jogo); na tela sintética e na
página de login a diferença ficou dentro do ruído. Baixar o zlib (compressão 1) **aumentou** os
bytes no TigerVNC, por isso o preset mantém zlib 2.

Em produção o RTT é 232 ms (medido daqui até a hel1), o dobro do laboratório: espere ~250–300 ms
do clique ao pixel com a imagem nova, contra ~350–450 ms antes, e a taxa de quadros deixa de
depender do RTT. O ganho da CPU (3,5 núcleos) vem por cima disso e não foi medido no laboratório.

## 9. Pendências e limitações conhecidas

- O RTT de 232 ms (Brasil ↔ Helsinque) é físico: metade dele fica em cada evento e em cada quadro,
  faça o que se fizer no servidor. Só mudar de região tira isso.
- O WebGL em software do jogo continua custando ~1,3 núcleo por slot; sem GPU não há como baixar
  sem mexer no jogo (qualidade de efeitos do próprio jogo é configuração do jogador).
- Arrastar a partir de texto selecionado vira drag-and-drop do Chromium e a página não vê o
  `mouseup` (também acontece localmente). Clicar em qualquer lugar solta.
- Limite de 2 clientes na tela remota por slot (`VNC_MAX_CLIENTS`), inalterado.
