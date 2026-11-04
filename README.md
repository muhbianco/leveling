# Leveling Keeper

Mantém a sessão do [Leveling Idle](https://levelingidle2d.com.br/jogar) aberta na hel1 enquanto o
navegador do jogador não pode ficar aberto. O painel fica em `https://leveling.muhbianco.com.br`, só
para os e-mails Google de `ADMIN_EMAILS` (dono) e `PLAYER_EMAILS` (jogadores liberados).

- **Uma instância por jogador, no máximo 2.** O jogador entra com o Google, informa caçador e senha
  do jogo e ganha a própria instância (Chromium, tela e tela remota próprios). Cada conta Google só
  vê e controla a própria. O teto de 2 é do jogo: acima de 2 contas pelo mesmo IP, ele derruba
  todas.
- **Várias contas do jogo por instância.** O jogador cadastra até 20 contas (caçador + senha) e
  escolhe qual a instância usa. Trocar de conta fecha o Chromium e apaga o perfil da instância
  (o token da conta anterior mora nele), então a próxima abertura entra com a conta nova. A chave
  liga/desliga não muda na troca. Salvar um caçador que já está na lista só troca a senha dele.
- **Uma conta ativa em uma instância só.** Se a conta já é a ativa da outra vaga (nome comparado
  sem diferenciar maiúsculas), ativar é recusado, e criar a instância com ela também: duas vagas na
  mesma conta gastariam as duas vagas de IP e uma derrubaria a outra.
- **Ligado:** o servidor abre o jogo num Chromium de verdade e fica conectado.
- **Desligado:** o navegador do servidor fecha.
- **Desliga sozinho** quando a conta entra em outro aparelho (`session.replaced`). Basta abrir o
  jogo no PC; lembrar de ligar quando sair.

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
- **Confirmação antes de entrar de novo.** O primeiro login depois de ligar é livre. Se o token
  cair depois de o robô já ter jogado nesse ciclo, ele segura o login por 15 s e abre um modal no
  painel. O título da aba conta os segundos, mesmo com a aba em segundo plano.
  - "Sou eu, desliga o keeper": desliga sem entrar.
  - "Não sou eu, reconectar": entra na hora.
  - Sem resposta: entra de novo.
  Ninguém vê o outro aparelho, então a dúvida não se resolve sozinha.
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
  Playwright → Chromium com janela num Xvfb próprio (slot 1 em `:99`, slot 2 em `:100`) e x11vnc
  só em `127.0.0.1` (5900, 5901). A ponte `app/vnc.py` liga o WebSocket `/vnc/ws` ao x11vnc do
  slot da sessão, depois de conferir sessão e Origin.
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
- **Env da stack:** `LEVELING_TAG`, `GOOGLE_CLIENT_ID`, `ADMIN_EMAILS` e `PLAYER_EMAILS`. Mudar
  jogadores: `portainer-stack-update.py --stack leveling --yaml ... --set-env PLAYER_EMAILS=...`
  (reinicia o container; as instâncias ligadas voltam sozinhas).

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
| Rollback | `infra/deploy.sh <sha12 anterior>` (com `PORTAINER_STACK_UPDATE` apontando para o script) |
| Desligar tudo | `docker service scale leveling_keeper=0` (religar com `=1`) |
| Medir consumo | `docker stats $(docker ps -q -f name=leveling_keeper)`. ~1 núcleo e ~750 MB por instância jogando; limite 2 CPU / 3 GB |
| Logs | `docker service logs leveling_keeper`. Nunca contêm senha; o login loga só o tipo do erro |
