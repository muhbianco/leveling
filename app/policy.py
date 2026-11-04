"""Decisão pura: dado o que se vê no navegador e a vontade do dono, o que fazer agora.

Sem I/O e sem relógio próprio, para a tabela inteira caber num teste. O executor
(`supervisor.py`) observa, chama `decide` e cumpre a ação.

Regra de ouro: o robô só fica conectado. Nada de clique dentro do jogo; a única interação é o
login quando o token cai. Erros que insistir não resolve (outro aparelho, inatividade, conta
suspensa, desconectado pelo servidor) desligam a chave em vez de tentar de novo.
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
# Token caiu depois de já ter jogado: quase sempre é o dono entrando em outro aparelho (o jogo às
# vezes mostra "Sessão expirada" em vez de "acessada em outro dispositivo"). Entrar com a senha na
# hora derrubaria ele; o painel pede confirmação e, sem resposta nesse prazo, o robô entra de novo.
CONFIRM_WINDOW_SECONDS = 15.0


class Phase(StrEnum):
    DESLIGADO = "desligado"
    ABRINDO = "abrindo"
    ENTRANDO = "entrando"
    JOGANDO = "jogando"
    RECONECTANDO = "reconectando"
    MANUTENCAO = "manutencao"
    PRECISA_LOGIN = "precisa_login"
    ASSUMIDO = "assumido"
    CONFIRMANDO = "confirmando"  # segurando o login à espera do dono (CONFIRM_WINDOW_SECONDS)
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
    HOLD = "hold"  # segura o login e abre a janela de confirmação no painel


class Relogin(StrEnum):
    """Pode entrar com a senha agora? Decidido pelo supervisor, que sabe se já jogou."""

    FREE = "free"  # primeiro login depois de ligar, ou a janela passou
    ASK = "ask"  # já jogou e o token caiu: abrir a janela de confirmação
    WAIT = "wait"  # janela aberta: esperando o dono


@dataclass(frozen=True, slots=True)
class Observation:
    screen: Screen
    block_kind: BlockKind | None = None
    block_text: str = ""
    signal_age: float | None = None  # segundos desde o último quadro do WebSocket do jogo
    screen_age: float = 0.0  # segundos na tela atual
    crashed: bool = False


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
        if not ctx.has_credentials:
            return Decision(Action.NONE, Phase.PRECISA_LOGIN, "sem usuário e senha configurados")
        if ctx.login_latched:
            return Decision(Action.NONE, Phase.PRECISA_LOGIN, "login recusado; confira a senha")
        if ctx.manual:
            return Decision(Action.NONE, Phase.ENTRANDO)
        held = _hold_relogin(ctx, "a sessão caiu depois de jogar")
        if held is not None:
            return held
        if not ctx.login_allowed:
            return Decision(Action.NONE, Phase.ENTRANDO)
        return Decision(Action.LOGIN, Phase.ENTRANDO, "token caiu; entrando de novo")

    if obs.screen is Screen.GAME:
        if obs.signal_age is not None and obs.signal_age <= SIGNAL_STALE_SECONDS:
            return Decision(Action.NONE, Phase.JOGANDO)
        dead = obs.signal_age is None or obs.signal_age > SIGNAL_DEAD_SECONDS
        if dead and obs.screen_age > SIGNAL_DEAD_SECONDS:
            return _gated(Action.RELOAD, Phase.RECONECTANDO, ctx, "sem sinal do servidor do jogo")
        return Decision(Action.NONE, Phase.RECONECTANDO)

    # LOADING
    if obs.screen_age > LOADING_GRACE_SECONDS:
        return _gated(Action.RELOAD, Phase.ABRINDO, ctx, "a página não terminou de carregar")
    return Decision(Action.NONE, Phase.ABRINDO)


def _hold_relogin(ctx: Context, reason: str) -> Decision | None:
    if ctx.relogin is Relogin.ASK:
        return Decision(Action.HOLD, Phase.CONFIRMANDO, reason)
    if ctx.relogin is Relogin.WAIT:
        return Decision(Action.NONE, Phase.CONFIRMANDO, reason)
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
            # Recarregar sem token cai na tela de login; nenhum clique no bloqueio. A janela de
            # confirmação abre já aqui, para o dono ter os 15 s desde a queda.
            held = None if ctx.manual else _hold_relogin(ctx, text)
            if held is not None:
                return held
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
