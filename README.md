# Leveling Keeper

Mantém a sessão do [Leveling Idle](https://levelingidle2d.com.br/jogar) aberta na hel1 enquanto o
navegador do jogador não pode ficar aberto. O painel fica em `https://leveling.muhbianco.com.br`, só
para os e-mails Google de `ADMIN_EMAILS` (dono) e `PLAYER_EMAILS` (jogadores liberados).

- **Uma instância por jogador, no máximo 3 (`MAX_SLOTS`).** O jogador entra com o Google, informa
  caçador e senha do jogo e ganha a própria instância (Chromium, tela e tela remota próprios).
  Cada conta Google só vê e controla a própria.
- **Slots e IPs.** O jogo derruba todas as contas acima de 2 pelo mesmo IP. Os slots 1 e 2 saem
  pelo IP principal da hel1; o slot 3 manda só o tráfego do servidor do jogo
  (`game.levelingidle2d.com.br:8443`) por um proxy SOCKS5 nosso (`app/egress.py`, serviço
  `egress` da stack) que sai pelo floating IP da Hetzner. Isso vale para **pessoas diferentes, uma
  conta cada, sem troca de itens ou gold entre as contas**: foi o que o dev do jogo (Jumpy, Discord,
  06/11/2026) disse que não é multi-conta; o limite deles é por pessoa e eles verificam
  comportamento. Não é para uma pessoa com várias contas.
- **Várias contas do jogo por instância.** O jogador cadastra até 20 contas (caçador + senha) e
  escolhe qual a instância usa. Trocar de conta fecha o Chromium e apaga o perfil da instância
  (o token da conta anterior mora nele), então a próxima abertura entra com a conta nova. A chave
  liga/desliga não muda na troca. Salvar um caçador que já está na lista só troca a senha dele.
- **Uma conta ativa em uma instância só.** Se a conta já é a ativa da outra vaga (nome comparado
  sem diferenciar maiúsculas), ativar é recusado, e criar a instância com ela também: duas vagas na
  mesma conta gastariam as duas vagas de IP e uma derrubaria a outra.
- **Ligado:** o servidor abre o jogo num Chromium de verdade e fica conectado.
- **Desligado:** o navegador do servidor fecha.
- **Sai sozinho** quando a conta entra em outro aparelho: basta abrir o jogo no PC. O keeper
  nunca volta sozinho; ligue quando sair (ver "Quando o dono assume o jogo").

## Acesso à tela remota de outro jogador (`app/access.py`)

Os admins (`ADMIN_EMAILS`) veem, no painel, o card "Instâncias dos outros jogadores". Quem está
só em `PLAYER_EMAILS` vê só a própria instância. Há dois modos, e os dois pedem **tempo** (1 h,
4 h, 12 h ou sempre aberto) e **motivo**.

**Pedir acesso** (quem quer ver):

1. **Pedido.** O admin clica em "Pedir acesso", escolhe o tempo e escreve o motivo. O keeper manda
   ao WhatsApp do dono da instância (`PLAYER_PHONES`) um convite
   `https://leveling.muhbianco.com.br/acesso/<token>`, que vale **4 h**.
   - O envio é pela WuzAPI, na sessão do MegaPromoOz (a mesma do sesi-vagas).
   - O pedido aparece também no card "Acesso à sua instância" do painel do dono. Isso vale com o
     WhatsApp fora do ar e com o dono sem telefone cadastrado.
2. **Aceite.** O dono abre o convite. Sem sessão, ele passa pelo Google e volta para o link.
   - Ele aceita (vale o tempo pedido, **contado a partir do aceite**) ou recusa.
   - Quem aceita é a **sessão Google do dono**; o token só localiza o pedido. Um link encaminhado
     para outra conta mostra "outra conta" e não libera nada.
   - No disco fica só o hash do token.
3. **Uso.** Quem pediu recebe um WhatsApp com `/remota?slot=N` e abre com a sessão Google dele.

**Abrir minha instância** (o dono, por conta própria):

- No card "Acesso à sua instância", o dono escolhe para quem (um jogador ou **todos**), o tempo e o
  motivo. Quem recebe é avisado no WhatsApp.
- "Todos os jogadores" vale para qualquer admin, inclusive quem entrar depois.
- Abrir de novo para o mesmo destino troca o tempo e o motivo.

**Fim.** O acesso cai quando:

- o prazo vence ("sempre aberto" não vence);
- o dono fecha (no painel), ou quem recebeu abre mão do acesso pessoal;
- o dono apaga a instância;
- alguém sai da lista de e-mails.

A tela remota aberta reavalia a cada mudança no slot. Se ninguém mais cobre quem está vendo, ela
cai na hora (WebSocket fechado com 1008) e a página para de reconectar. Perder o acesso pessoal
com a instância ainda aberta para todos não derruba a tela.

Regras:

- **Um pedido por par de cada vez.** Um pedido recusado segura o próximo até o convite expirar,
  para o WhatsApp do dono não virar alvo de insistência. Quando o WhatsApp falha, dá para pedir de
  novo.
- **Eventos do dono:** pedido (com tempo e motivo), aceite, recusa, abertura, fechamento e cada
  abrir/fechar da tela por outro jogador.
- **Só a tela remota.** Ligar ou desligar, contas e apagar a instância continuam só com o dono.
  Enquanto alguém está na tela remota, o robô daquela instância só observa.
- **A conta do jogo do arquivo de segredos é só do dono** (o primeiro de `ADMIN_EMAILS`), mesmo
  com todos os jogadores admin.
- **Sem troca de itens ou gold entre as contas.** É a condição combinada com o dev do jogo
  (ver "Slots e IPs"). Usar o acesso para isso transforma pessoas diferentes em multi-conta.

## Quando o dono assume o jogo (`app/takeover.py`)

**Uma conta, um aparelho.** O jogo não deixa a mesma conta em dois lugares: um derruba o outro e,
repetindo, o jogo bloqueia a conta por minutos. Por isso, depois de já ter jogado neste ciclo, o
keeper **nunca entra com a senha sozinho**.

- **Qualquer queda da sessão conta como o dono assumindo:** "acessada em outro dispositivo",
  "Sessão expirada", ou recarga sem sinal que cai na tela de login. O keeper desliga
  ("assumido") e fecha o navegador na hora.
- **Por 5 min o dono pode mandar o keeper voltar:**
  - painel aberto (consultou o status há menos de 15 s): modal "Você assumiu o controle do
    jogo", com "Keeper assume" e "Estou jogando";
  - painel fechado: WhatsApp com o link `/retomar/<token>`, que pede a sessão Google do dono.
- **Sem resposta, nada acontece.** O keeper continua desligado e o dono segue jogando. Para ele
  voltar depois, é ligar pela chave.
- **O primeiro login depois de ligar é livre:** ligar (pela chave ou pelo "Keeper assume") é o dono
  entregando o jogo. Voltar de um restart (deploy) com a chave ligada não conta: o keeper retoma
  pelo token; se o token morreu no intervalo, ele sai e avisa, como numa queda.

**Regressão de 11 e 12/11/2026.** Antes havia uma janela de 15 s que, sem resposta, entrava com a
senha. Nas duas vezes ela derrubou o dono, e o jogo deixou a conta desconectada por minutos:

- às 22:34, com "Sessão expirada";
- às 06:14, com recarga sem sinal que caiu na tela de login.

**Ainda não medido:** se um reinício do servidor do jogo mata o token do keeper. Se matar, o
keeper também sai e espera o dono.

## Como o jogo funciona (lido do cliente em 02/11/2026)

- **Conexão:** Phaser 3 + WebSocket binário em `wss://game.levelingidle2d.com.br:8443/ws`. A caçada
  e as automações (Auto-helper, auto-continuar…) rodam no servidor do jogo; o cliente só precisa
  continuar conectado.
- **Uma sessão por conta:** entrar em outro aparelho derruba a anterior ("Sua conta foi acessada em
  outro dispositivo"). A sessão derrubada não reconecta sozinha.
- **Às vezes a sessão derrubada vê "Sessão expirada"** (`errors.session.expired`) em vez de "acessada
  em outro dispositivo". Foi o que aconteceu nas 3 vezes registradas até 04/11/2026: duas do dono e
  uma do pugtimothy, sempre com o jogador voltando ao jogo. Entrar com a senha nessa hora derruba o
  jogador.
- **Login:** usuário e senha pelo WebSocket, sem captcha (o Turnstile é só do cadastro).
- **Token:** fica em `leveling.session-token` no localStorage. Com o perfil persistente em `/data`,
  recarregar a página retoma a sessão sem senha.

## O que o robô faz e o que não faz

- **Só fica conectado.** Nenhum script injetado na página, nenhum clique dentro do jogo e nenhum
  disfarce: headers, user agent e marcadores de automação ficam como o Chromium manda.
- **Única interação:** preencher o login quando o token cai, no máximo 1 tentativa a cada 2 min
  (o jogo pode levar mais de 1 min carregando depois do login). Depois de 3 tentativas sem entrar,
  trava até a senha mudar ou o jogador clicar "Tentar login de novo".
- **O login confere antes de enviar.** O keeper só clica em "Entrar" com os dois campos iguais ao
  usuário e à senha; o que estiver diferente ele redigita (até 3 rodadas) e, se não firmar, não
  envia nada e avisa nos eventos.
- **O gerenciador de senhas do Chromium fica de fora.** A cada abertura o keeper apaga o cofre do
  perfil (`Default/Login Data*`) e desliga a oferta de "Salvar senha?" (`credentials_enable_service`
  no `Preferences`). O Playwright 1.64 não liga `--enable-automation`, então sem isso o gerenciador
  funciona como num Chromium comum. Em 10/11/2026 os slots 1 e 2 tinham a senha do jogo salva: o
  Chromium preenchia o formulário junto com o keeper e, quando caía entre o selecionar e o inserir
  do `fill`, o campo ficava com o texto duas vezes (medido na imagem do keeper, com senha salva e
  CPU apertada: 16 logins errados em 250; com a conferência, 0 em 250). Senha salva ali também
  fica no volume praticamente em claro (`--password-store=basic`), fora da cifragem das contas.
- **Depois de jogar, não entra com a senha sozinho.** A sessão caiu: desliga e oferece ao dono,
  por 5 min, o "Keeper assume" (ver "Quando o dono assume o jogo").
- **Não simula presença nem responde anti-AFK.** Se o jogo encerrar por inatividade
  (`session.idle`), ser desconectado pelo servidor, suspender a conta ou acusar multi-conta por IP,
  o robô **desliga** em vez de insistir.
- **Recargas gentis:** backoff de 30 s até 15 min, com jitter. Mais de 8 aberturas/recargas numa
  hora sem chegar a jogar → desliga com erro.
- **Tela remota aberta = modo manual:** o robô só observa (o auto-desligar continua valendo).
- **Se a caçada não continuar sozinha** depois que o servidor assume, retome pela tela remota.

## Arquitetura

Um container, até 2 slots (`app/slots.py`):

- FastAPI (painel, API e noVNC atrás da sessão Google). O slot de cada requisição sai do e-mail da
  sessão, nunca de um id vindo do navegador.
- Por slot: supervisor asyncio (`app/supervisor.py` executa; `app/policy.py` decide, função pura),
  Playwright → Chromium em tela cheia (`--start-fullscreen`: o jogo fica com os 1280x720
  inteiros; F11 na tela remota alterna) num Xvfb próprio (slot 1 em `:99`, slot 2 em `:100`) e
  X0tigervnc (TigerVNC) só em `127.0.0.1` (5900, 5901). A ponte `app/vnc.py` liga o WebSocket
  `/vnc/ws` ao servidor VNC do slot da sessão, depois de conferir sessão e Origin.
- Tela remota: `/remota` é um visor próprio sobre o núcleo do noVNC (`app/web/remota.*`): barra
  compacta, rolagem sem roda, "segurar" o botão para arrastar, Esc, qualidade nítida/rápida,
  ajustar/1:1, tela cheia e reconexão. A UI padrão do noVNC fica de reserva em `/vnc/padrao`.
  Por que TigerVNC e não x11vnc, com as medições: `docs/tela-remota.md`. Em resumo: XDAMAGE em vez
  de varrer o framebuffer, quadros sem esperar pedido (a taxa não depende dos 230 ms de ida e
  volta até o jogador) e teto de `VNC_FRAME_RATE` quadros/s (30).
- Contas do jogo: o jogador cadastra pelo painel. A senha fica no volume cifrada com Fernet, e a
  chave (`DATA_KEY`) fica só no arquivo de segredos da hel1. Para o dono, o
  `GAME_USERNAME`/`GAME_PASSWORD` do arquivo de segredos aparece como mais uma conta na lista
  (id fixo `servidor`, não sai pelo painel). Sem escolha gravada, o dono usa essa conta.
- O supervisor relê a conta ativa a cada ciclo. Na troca, o `SlotManager` segura o laço do
  supervisor (`Supervisor.hold`) enquanto fecha o navegador, apaga o perfil e grava a conta nova.
  Se o perfil não sai do disco, a troca falha (503) e a conta anterior continua.

Sinais, todos passivos:

- **WebSocket do jogo:** eventos CDP. Mais de 30 s sem quadro do servidor = conexão em dúvida; mais
  de 90 s = recarrega.
- **DOM:** a sonda só lê. `.slScreen.slAuth` é login, `.slScreen.slBlock` é bloqueio e
  `#game canvas` é jogo.
- **Sonda sem resposta:** acontece com a CPU no teto (em 04/11/2026, 2,08 de 2 núcleos e 82% dos
  períodos estrangulados). Se a tela era de jogo e o WebSocket continua mandando quadros, a tela
  continua sendo de jogo. Antes ela virava "carregando", a fase pulava jogando ↔ abrindo a cada
  poucos segundos e o jogo era recarregado depois de 90 s.
- **Textos dos bloqueios:** vêm das respostas de `/locales/pt-BR/{auth,errors}.json` que o próprio
  jogo baixa. Assim não quebram se o texto mudar.

Estado em `/data` (arquivos com permissão 600):

- `slots.json`: quem ocupa cada vaga.
- `sessions.json`: hash das sessões do painel.
- `access.json`: pedidos de acesso (hash do token do link) e acessos liberados, com prazo.
- `slots/<n>/`: `state.json` (chave, motivo e últimos 200 eventos), `profile/` (perfil do Chromium)
  e `accounts.json` (contas do jogo com senha cifrada, mais qual está ativa). O `credentials.json`
  de antes (um caçador por vaga) vira a conta ativa no `accounts.json` no primeiro boot.
- O layout de antes dos slots (`/data/state.json` + `/data/profile`) vira o slot 1 do primeiro
  e-mail de `ADMIN_EMAILS` no primeiro boot, com eventos e token do jogo preservados.

## Desenvolvimento

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy app && .venv/Scripts/python -m pytest -q
```

O navegador real (`app/game.py`) é validado no container, não por mock. Imagem local (Docker via
WSL):

```bash
wsl -e docker build -t leveling-keeper:local .
```

## Produção (hel1)

- **Repo:** `github.com/muhbianco/leveling` (a pasta local se chama `leveling-keeper`). Clone na
  hel1 em `/usr/src/leveling`, só para rodar os scripts de `infra/`.
- **Stack e imagem:** stack `leveling`, serviço `leveling_keeper`, rede `chatbot-net`, imagem
  `muhrilobianco/leveling-keeper:<sha12>`.
- **Deploy:** push na `main` = deploy (Woodpecker: `.woodpecker/ci.yaml` → `deploy.yaml` →
  `infra/deploy.sh`).
- **Volume:** `leveling_data` fica **fora do backup B2** de propósito. É só perfil, chave, eventos e
  credenciais cifradas; um login novo recupera tudo.
- **Env da stack:** `LEVELING_TAG`, `GOOGLE_CLIENT_ID`, `ADMIN_EMAILS` (o dono primeiro),
  `PLAYER_EMAILS` e `PLAYER_PHONES` (`email=telefone`, vírgula). Mudar jogadores ou telefones:
  `portainer-stack-update.py --stack leveling --yaml ... --set-env PLAYER_EMAILS=...` (reinicia o
  container; as instâncias ligadas voltam sozinhas).
- **WhatsApp do pedido de acesso:** `install-secrets.sh --wuzapi` copia `WUZAPI_BASE_URL` e
  `WUZAPI_SESSION_TOKEN` de `/root/.megapromooz-wuzapi.env` para o arquivo de segredos (sem
  pedir nada; o token de admin não é copiado). O app relê o arquivo a cada envio.

### Primeira vez

1. **Google Cloud:** cliente OAuth "Leveling Keeper" (tipo Web) com redirect
   `https://leveling.muhbianco.com.br/auth/callback`. Se a tela de consentimento estiver em
   "Testing", inclua o e-mail do dono como testador.
2. **Woodpecker:** ativar `muhbianco/leveling` (Trusted → Volumes) e rodar o pipeline da `main`.
   O passo `build` publica `muhrilobianco/leveling-keeper:<sha12>`. O `deploy` falha até a stack
   existir; é esperado.
3. **Clone e segredos na hel1** (o script pergunta cada valor sem eco; Enter mantém o atual; a
   `DATA_KEY` é gerada sozinha, uma vez):
   ```bash
   git clone https://github.com/muhbianco/leveling.git /usr/src/leveling
   bash /usr/src/leveling/infra/install-secrets.sh
   ```
4. **Criar a stack** com o sha12 do passo 2:
   ```bash
   python3 /usr/src/hel1-ops/scripts/portainer-stack-update.py --stack leveling --create \
     --yaml /usr/src/leveling/infra/docker-stack.yml \
     --set-env LEVELING_TAG=<sha12> --set-env GOOGLE_CLIENT_ID=<id> \
     --set-env ADMIN_EMAILS=<email do dono>
   ```
   Daí em diante, push na `main` faz o deploy sozinho.

### Operação

| Tarefa | Como |
|---|---|
| Cadastrar, trocar ou remover contas do jogo | o próprio jogador, em "Contas do jogo" no painel (a ativa não sai; trocar antes) |
| Conta recusada com "já está ativa na outra instância" | quem está com ela ativa troca de conta na própria instância |
| Trocar a senha do dono (arquivo) ou o segredo Google | rodar `install-secrets.sh` de novo; o app relê sozinho |
| Garantir a `DATA_KEY` sem perguntar nada | `install-secrets.sh --ensure-data-key` (nunca troca uma existente) |
| Liberar a vaga de alguém | o próprio jogador em "Apagar minha instância"; tirar o e-mail de `PLAYER_EMAILS` só para de rodar a instância |
| Liberar ou tirar o painel compartilhado de alguém | mover o e-mail entre `ADMIN_EMAILS` e `PLAYER_EMAILS` (o dono fica sempre primeiro em `ADMIN_EMAILS`) |
| Pedido de acesso sem WhatsApp | conferir `PLAYER_PHONES` do dono da instância e os `WUZAPI_*` do arquivo; o evento do dono diz qual foi ("sem WhatsApp cadastrado", "o WhatsApp falhou"…) |
| Rollback | `infra/deploy.sh <sha12 anterior>` (com `PORTAINER_STACK_UPDATE` apontando para o script). Para antes do acesso compartilhado: **primeiro** deixar só o dono em `ADMIN_EMAILS` (a imagem antiga dá a conta do arquivo de segredos a todo admin), depois trocar a imagem |
| Desligar tudo | `docker service scale leveling_keeper=0` (religar com `=1`) |
| Medir consumo | `docker stats $(docker ps -q -f name=leveling_keeper)`. ~1 núcleo e ~750 MB por instância jogando; limite 2 CPU / 3 GB |
| Logs | `docker service logs leveling_keeper`. Nunca contêm senha; o login loga só o tipo do erro |
