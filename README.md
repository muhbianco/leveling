# Leveling Keeper

Mantém a sessão do [Leveling Idle](https://levelingidle2d.com.br/jogar) aberta na hel1 enquanto o
navegador do dono não pode ficar aberto. O painel fica em `https://leveling.muhbianco.com.br`, só
para os e-mails Google de `ADMIN_EMAILS`.

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
- **Login:** usuário e senha pelo WebSocket, sem captcha (o Turnstile é só do cadastro).
- **Token:** fica em `leveling.session-token` no localStorage. Com o perfil persistente em `/data`,
  recarregar a página retoma a sessão sem senha.

## O que o robô faz e o que não faz

- **Só fica conectado.** Nenhum script injetado na página, nenhum clique dentro do jogo e nenhum
  disfarce: headers, user agent e marcadores de automação ficam como o Chromium manda.
- **Única interação:** preencher o login quando o token cai, no máximo 1 tentativa por minuto. Depois
  de 3 tentativas sem entrar, trava até o dono trocar a senha ou clicar "Tentar login de novo".
- **Não simula presença nem responde anti-AFK.** Se o jogo encerrar por inatividade
  (`session.idle`), ser desconectado pelo servidor, suspender a conta ou acusar multi-conta por IP,
  o robô **desliga** em vez de insistir.
- **Recargas gentis:** backoff de 30 s até 15 min, com jitter. Mais de 8 aberturas/recargas numa
  hora sem chegar a jogar → desliga com erro.
- **Tela remota aberta = modo manual:** o robô só observa (o auto-desligar continua valendo).
- **Se a caçada não continuar sozinha** depois que o servidor assume, retome pela tela remota.

## Arquitetura

Um container:

- FastAPI (painel, API e noVNC atrás da sessão Google).
- Supervisor asyncio (`app/supervisor.py` executa; `app/policy.py` decide, função pura).
- Playwright → Chromium com janela num Xvfb (`:99`).
- x11vnc só em `127.0.0.1:5900`. A ponte `app/vnc.py` liga o WebSocket `/vnc/ws` a ele, depois de
  conferir sessão e Origin.

Sinais, todos passivos:

- **WebSocket do jogo:** eventos CDP. Mais de 30 s sem quadro do servidor = conexão em dúvida; mais
  de 90 s = recarrega.
- **DOM:** a sonda só lê. `.slScreen.slAuth` é login, `.slScreen.slBlock` é bloqueio e
  `#game canvas` é jogo.
- **Textos dos bloqueios:** vêm das respostas de `/locales/pt-BR/{auth,errors}.json` que o próprio
  jogo baixa. Assim não quebram se o texto mudar.

Estado em `/data`:

- `state.json`: chave, motivo e últimos 200 eventos.
- `sessions.json`: hash das sessões do painel.
- `profile/`: perfil do Chromium.

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
- **Volume:** `leveling_data` fica **fora do backup B2** de propósito. É só perfil, chave e eventos,
  e um login novo recupera tudo.

### Primeira vez

1. **Google Cloud:** cliente OAuth "Leveling Keeper" (tipo Web) com redirect
   `https://leveling.muhbianco.com.br/auth/callback`. Se a tela de consentimento estiver em
   "Testing", inclua o e-mail do dono como testador.
2. **Woodpecker:** ativar `muhbianco/leveling` (Trusted → Volumes) e rodar o pipeline da `main`.
   O passo `build` publica `muhrilobianco/leveling-keeper:<sha12>`. O `deploy` falha até a stack
   existir; é esperado.
3. **Clone e segredos na hel1** (o script pergunta cada valor sem eco; Enter mantém o atual):
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
| Trocar a senha do jogo ou o segredo Google | rodar `install-secrets.sh` de novo; o app relê sozinho |
| Rollback | `infra/deploy.sh <sha12 anterior>` (com `PORTAINER_STACK_UPDATE` apontando para o script) |
| Desligar tudo | `docker service scale leveling_keeper=0` (religar com `=1`) |
| Medir consumo | `docker stats $(docker ps -q -f name=leveling_keeper)`. Limite atual: 1 CPU / 1536 MB |
| Logs | `docker service logs leveling_keeper`. Nunca contêm senha; o login loga só o tipo do erro |
