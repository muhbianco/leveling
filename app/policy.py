"""Decisão pura: dado o que se vê no navegador e a vontade do dono, o que fazer agora.

Sem I/O e sem relógio próprio, para a tabela inteira caber num teste. O executor
(`supervisor.py`) observa, chama `decide` e cumpre a ação.

Regra de ouro: o robô só fica conectado. Nada de clique dentro do jogo; a única interação é o
login quando o token cai. Erros que insistir não resolve (outro aparelho, inatividade, conta
suspensa, desconectado pelo servidor) desligam a chave em vez de tentar de novo.

Uma conta, um aparelho: depois de já ter jogado neste ciclo, o keeper nunca entra com a senha
sozinho. Qualquer queda da sessão (outro aparelho, "sessão expirada", recarga que cai no login) é
tratada como o dono assumindo o jogo: o keeper desliga ("assumido") e quem decide voltar é o dono
(app/takeover.py). Regressão de 11 e 12/11/2026: a janela de 15 s que reconectava sozinha derrubou o
dono duas vezes e o jogo bloqueou a conta por minutos.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.state import OffReason

SIGNAL_STALE_SECONDS = 30.0  # sem quadro do servidor há mais que isso: conexão em dúvida
SIGNAL_DEAD_SECONDS = 90.0  # o cliente teve tempo de reconectar sozinho e não conseguiu
LOADING_GRACE_SECONDS = 90.0  # página aberta sem chegar a login, bloqueio ou jogo
BLOCK_GRACE_SECONDS = 60.0  # bloqueio desconhecido: espera antes de recarregar
MAINTENANCE_GRACE_SECONDS = 600.0  # manutenção: o cliente volta sozinho; depois disso, recarrega
MAX_ATTEMPTS_PER_HOUR = 8  # aberturas/recargas sem chegar a jogar: passou disso, desliga


class Phase(StrEnum):
    DESLIGADO = "desligado"
    ABRINDO = "abrindo"
    ENTRANDO = "entrando"
    JOGANDO = "jogando"
    RECONECTANDO = "reconectando"
    MANUTENCAO = "manutencao"
    PRECISA_LOGIN = "precisa_login"
    ASSUMIDO = "assumido"
    ERRO = "erro"


class Screen(StrEnum):
    CLOSED = "closed"  # sem navegador
    LOADING = "loading"  # carregando ou tela que não reconhecemos
    LOGIN = "login"  # tela inicial ou formulário de login
    BLOCK = "block"  # bloqueio do jogo (sessão encerrada, manutenção, falha)
    GAME = "game"  # dentro do jogo


class BlockKind(StrEnum):
    REPLACED = "replaced"  # entrou em outro aparelho
    STOP = "stop"  # não adianta insistir: inatividade, suspensa, multi-conta, kick do servidor
    RELOGIN = "relogin"  # sessão expirada: recarregar leva ao login
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"


class Action(StrEnum):
    NONE = "none"
    LAUNCH = "launch"  # abre o Chromium e a página do jogo
    RELOAD = "reload"  # volta para a página do jogo (relança se o navegador caiu)
    LOGIN = "login"  # preenche usuário e senha e envia
    CLOSE = "close"  # fecha o navegador
    TURN_OFF = "turn_off"  # desliga a chave e fecha


class Relogin(StrEnum):
    """Pode entrar com a senha agora? Decidido pelo supervisor, que sabe se já jogou."""

    FREE = "free"  # primeiro login depois de ligar: o dono acabou de entregar o jogo ao keeper
    YIELD = "yield"  # já jogou e a sessão caiu: é o dono assumindo; o keeper sai


@dataclass(frozen=True, slots=True)
class Observation:
    screen: Screen
    block_kind: BlockKind | None = None
    block_text: str = ""
    signal_age: float | None = None  # segundos desde o último quadro do WebSocket do jogo
    screen_age: float = 0.0  # segundos na tela atual
    crashed: bool = False
    token: bool = False  # o jogo tem uma sessão salva (o valor nunca sai da página)


@dataclass(frozen=True, slots=True)
class Context:
    desired_on: bool
    off_reason: OffReason = OffReason.MANUAL
    manual: bool = False  # tela remota conectada: o dono está mexendo, o robô só observa
    has_credentials: bool = True
    login_latched: bool = False  # login recusado repetidas vezes: espera o dono
    login_allowed: bool = True  # espaçamento entre tentativas de login
    retry_ready: bool = True  # backoff de abrir/recarregar vencido
    attempts_last_hour: int = 0
    relogin: Relogin = Relogin.FREE
    login_recent: bool = False  # login enviado há pouco: o jogo ainda está decidindo


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    phase: Phase
    reason: str = ""
    off_reason: OffReason | None = None  # só com TURN_OFF


def _gated(action: Action, phase: Phase, ctx: Context, reason: str) -> Decision:
    """Abrir/recarregar respeita backoff, modo manual e o teto por hora."""
    if ctx.manual:
        return Decision(Action.NONE, phase, reason)
    if ctx.attempts_last_hour >= MAX_ATTEMPTS_PER_HOUR:
        return Decision(
            Action.TURN_OFF,
            Phase.ERRO,
            f"{ctx.attempts_last_hour} tentativas na última hora sem conseguir jogar",
            OffReason.ERROR,
        )
    if not ctx.retry_ready:
        return Decision(Action.NONE, phase, reason)
    return Decision(action, phase, reason)


def decide(obs: Observation, ctx: Context) -> Decision:
    if not ctx.desired_on:
        phase = {
            OffReason.REPLACED: Phase.ASSUMIDO,
            OffReason.ERROR: Phase.ERRO,
        }.get(ctx.off_reason, Phase.DESLIGADO)
        if obs.screen is not Screen.CLOSED:
            return Decision(Action.CLOSE, phase)
        return Decision(Action.NONE, phase)

    if obs.screen is Screen.CLOSED:
        return _gated(Action.LAUNCH, Phase.ABRINDO, ctx, "abrindo o navegador")

    if obs.crashed:
        return _gated(Action.RELOAD, Phase.RECONECTANDO, ctx, "a página do jogo caiu")

    if obs.screen is Screen.BLOCK:
        return _decide_block(obs, ctx)

    if obs.screen is Screen.LOGIN:
        if ctx.manual:
            return Decision(Action.NONE, Phase.ENTRANDO)
        given_up = _yield(ctx, "a sessão do keeper caiu depois de jogar")
        if given_up is not None:
            return given_up
        if obs.token:
            # O jogo aceitou o login (gravou a sessão) e continuou na tela de acesso: o "Erro
            # desconhecido" que aparece quando a conta ainda estava ativa em outro lugar (medido em
            # 12/11/2026). Recarregar retoma pela sessão salva em ~5 s; digitar a senha de novo
            # (o que acontecia 2 min depois) é mais um login por cima.
            if ctx.login_recent:
                return Decision(Action.NONE, Phase.ENTRANDO, "entrando no jogo")
            return _gated(
                Action.RELOAD,
                Phase.ENTRANDO,
                ctx,
                "o jogo aceitou o login e não entrou; recarregando para retomar a sessão",
            )
        if not ctx.has_credentials:
            return Decision(Action.NONE, Phase.PRECISA_LOGIN, "sem usuário e senha configurados")
        if ctx.login_latched:
            return Decision(Action.NONE, Phase.PRECISA_LOGIN, "login recusado; confira a senha")
        if not ctx.login_allowed:
            return Decision(Action.NONE, Phase.ENTRANDO)
        # O jogo apaga a sessão salva a cada fim de sessão (outro aparelho, expirada): ligar
        # depois de jogar em outro lugar sempre passa por aqui. Não é falha.
        return Decision(Action.LOGIN, Phase.ENTRANDO, "entrando com usuário e senha")

    if obs.screen is Screen.GAME:
        if obs.signal_age is not None and obs.signal_age <= SIGNAL_STALE_SECONDS:
            return Decision(Action.NONE, Phase.JOGANDO)
        dead = obs.signal_age is None or obs.signal_age > SIGNAL_DEAD_SECONDS
        if dead and obs.screen_age > SIGNAL_DEAD_SECONDS:
            # Depois de jogar, 90 s de silêncio é o dono entrando em outro aparelho: desde
            # 11/11/2026 o servidor do jogo para de mandar dados à sessão antiga sem avisar (o
            # outro aparelho vê "Você já está conectado" até conseguir entrar). Queda de rede
            # volta sozinha em menos de 60 s (13 casos medidos em 12/11). Recarregar retomaria
            # pelo token e tomaria a sessão de volta: o keeper sai em vez disso.
            given_up = (
                None
                if ctx.manual
                else _yield(ctx, "o jogo parou de mandar dados (você entrou em outro aparelho?)")
            )
            if given_up is not None:
                return given_up
            return _gated(Action.RELOAD, Phase.RECONECTANDO, ctx, "sem sinal do servidor do jogo")
        return Decision(Action.NONE, Phase.RECONECTANDO)

    # LOADING
    if obs.screen_age > LOADING_GRACE_SECONDS:
        return _gated(Action.RELOAD, Phase.ABRINDO, ctx, "a página não terminou de carregar")
    return Decision(Action.NONE, Phase.ABRINDO)


def _yield(ctx: Context, reason: str) -> Decision | None:
    """Já jogou e a sessão caiu: o keeper desliga em vez de entrar com a senha."""
    if ctx.relogin is Relogin.YIELD:
        return Decision(Action.TURN_OFF, Phase.ASSUMIDO, reason, OffReason.REPLACED)
    return None


def _decide_block(obs: Observation, ctx: Context) -> Decision:
    text = obs.block_text[:200]
    match obs.block_kind:
        case BlockKind.REPLACED:
            # Mesmo com a tela remota aberta: dois aparelhos se derrubando é o pior caso.
            return Decision(Action.TURN_OFF, Phase.ASSUMIDO, text, OffReason.REPLACED)
        case BlockKind.STOP:
            return Decision(Action.TURN_OFF, Phase.ERRO, text, OffReason.ERROR)
        case BlockKind.RELOGIN:
            # Depois de jogar, "sessão expirada" é o dono entrando em outro aparelho (medido em
            # 04/11 e 11/11/2026): sai já, sem recarregar. Antes de jogar (token velho ao ligar),
            # recarregar leva ao login; nenhum clique no bloqueio.
            given_up = None if ctx.manual else _yield(ctx, text)
            if given_up is not None:
                return given_up
            return _gated(Action.RELOAD, Phase.ENTRANDO, ctx, text)
        case BlockKind.MAINTENANCE:
            if obs.screen_age > MAINTENANCE_GRACE_SECONDS:
                return _gated(Action.RELOAD, Phase.MANUTENCAO, ctx, text)
            return Decision(Action.NONE, Phase.MANUTENCAO, text)
        case _:
            if obs.screen_age > BLOCK_GRACE_SECONDS:
                return _gated(Action.RELOAD, Phase.RECONECTANDO, ctx, text)
            return Decision(Action.NONE, Phase.RECONECTANDO, text)


# --- Bloqueios: textos do próprio jogo (locales/pt-BR) ---------------------------------------

# Chave do locale -> tipo. A lista padrão foi lida do cliente em 02/11/2026; a cada abertura o
# supervisor troca pelos textos atuais do jogo (mesmas chaves), para não quebrar se mudarem.
BLOCK_KEYS: dict[str, BlockKind] = {
    "auth.session.ended_duplicate": BlockKind.REPLACED,
    "errors.session.replaced": BlockKind.REPLACED,
    "errors.session.idle": BlockKind.STOP,
    "errors.session.kicked": BlockKind.STOP,
    "errors.account.banned": BlockKind.STOP,
    "errors.session.multi_account": BlockKind.STOP,
    "errors.session.multi_account_ip": BlockKind.STOP,
    "errors.session.account_missing": BlockKind.STOP,
    "errors.session.expired": BlockKind.RELOGIN,
    "errors.session.conflict": BlockKind.RELOGIN,
    "errors.session.maintenance": BlockKind.MAINTENANCE,
    "errors.session.shutdown": BlockKind.MAINTENANCE,
    "auth.maintenance.title": BlockKind.MAINTENANCE,
    "auth.maintenance.text_line1": BlockKind.MAINTENANCE,
}

DEFAULT_TEXTS: dict[str, str] = {
    "auth.session.ended_duplicate": (
        "Sua conta foi acessada em outro dispositivo. Esta sessão foi encerrada por segurança."
    ),
    "errors.session.replaced": "Sessão encerrada — sua conta entrou em outro lugar.",
    "errors.session.idle": "Sessão encerrada por inatividade.",
    "errors.session.kicked": "Você foi desconectado pelo servidor.",
    "errors.account.banned": "Esta conta foi suspensa.",
    "errors.session.multi_account": (
        "Limite de {max} contas no jogo ao mesmo tempo por IP. Burlar essa regra pode levar ao "
        "banimento da sua conta."
    ),
    "errors.session.multi_account_ip": (
        "Mais de 2 contas no jogo pela mesma internet (IP): todas foram desconectadas."
    ),
    "errors.session.account_missing": "Conta não encontrada. Entre novamente.",
    "errors.session.expired": "Sessão expirada. Entre novamente.",
    "errors.session.conflict": (
        "Sua sessão foi recarregada para proteger seu progresso. Entre novamente."
    ),
    "errors.session.maintenance": "Servidor em manutenção. Volte em instantes.",
    "errors.session.shutdown": "O servidor está reiniciando. Entre novamente em instantes.",
    "auth.maintenance.title": "SERVIDOR EM MANUTENCAO",
    "auth.maintenance.text_line1": "Estamos realizando uma manutencao administrativa.",
}

_PLACEHOLDER = re.compile(r"\{[^}]*\}")
_SPACES = re.compile(r"\s+")
MIN_CHUNK = 8


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _SPACES.sub(" ", plain).strip().casefold()


@dataclass(frozen=True, slots=True)
class BlockMatcher:
    """Frases do jogo em pedaços fixos (sem os {placeholders}), em ordem de prioridade."""

    patterns: tuple[tuple[BlockKind, tuple[str, ...]], ...]

    @classmethod
    def from_texts(cls, texts: Mapping[str, str]) -> BlockMatcher:
        merged = {**DEFAULT_TEXTS, **{k: v for k, v in texts.items() if k in BLOCK_KEYS and v}}
        order = list(BlockKind)
        patterns: list[tuple[BlockKind, tuple[str, ...]]] = []
        for key, kind in sorted(BLOCK_KEYS.items(), key=lambda kv: order.index(kv[1])):
            chunks = tuple(
                c
                for c in (_normalize(p) for p in _PLACEHOLDER.split(merged[key]))
                if len(c) >= MIN_CHUNK
            )
            if chunks:
                patterns.append((kind, chunks))
        return cls(tuple(patterns))

    def classify(self, block_text: str) -> BlockKind:
        text = _normalize(block_text)
        for kind, chunks in self.patterns:
            if all(chunk in text for chunk in chunks):
                return kind
        return BlockKind.UNKNOWN


def flatten_locale(data: Mapping[str, object], prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, Mapping):
            out.update(flatten_locale(value, f"{prefix}{key}."))
        elif isinstance(value, str):
            out[f"{prefix}{key}"] = value
    return out


def locale_texts(files: Iterable[Mapping[str, object]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for data in files:
        merged.update(flatten_locale(data))
    return {k: v for k, v in merged.items() if k in BLOCK_KEYS}
