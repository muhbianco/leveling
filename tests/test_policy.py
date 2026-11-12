from __future__ import annotations

import pytest

from app.policy import (
    MAX_ATTEMPTS_PER_HOUR,
    Action,
    BlockKind,
    BlockMatcher,
    Context,
    Observation,
    Phase,
    Relogin,
    Screen,
    decide,
    locale_texts,
)
from app.state import OffReason

ON = Context(desired_on=True)
PLAYING = Observation(Screen.GAME, signal_age=1.0, screen_age=600.0)


def test_off_closes_open_browser() -> None:
    d = decide(PLAYING, Context(desired_on=False))
    assert (d.action, d.phase) == (Action.CLOSE, Phase.DESLIGADO)


@pytest.mark.parametrize(
    ("reason", "phase"),
    [
        (OffReason.MANUAL, Phase.DESLIGADO),
        (OffReason.REPLACED, Phase.ASSUMIDO),
        (OffReason.ERROR, Phase.ERRO),
    ],
)
def test_off_phase_keeps_why(reason: OffReason, phase: Phase) -> None:
    d = decide(Observation(Screen.CLOSED), Context(desired_on=False, off_reason=reason))
    assert (d.action, d.phase) == (Action.NONE, phase)


def test_on_without_browser_launches() -> None:
    assert decide(Observation(Screen.CLOSED), ON).action is Action.LAUNCH


def test_launch_waits_for_backoff() -> None:
    d = decide(Observation(Screen.CLOSED), Context(desired_on=True, retry_ready=False))
    assert (d.action, d.phase) == (Action.NONE, Phase.ABRINDO)


def test_too_many_attempts_turns_off_instead_of_insisting() -> None:
    ctx = Context(desired_on=True, attempts_last_hour=MAX_ATTEMPTS_PER_HOUR)
    d = decide(Observation(Screen.CLOSED), ctx)
    assert (d.action, d.off_reason) == (Action.TURN_OFF, OffReason.ERROR)


def test_playing_with_fresh_signal_does_nothing() -> None:
    d = decide(PLAYING, ON)
    assert (d.action, d.phase) == (Action.NONE, Phase.JOGANDO)


def test_stale_signal_gives_client_time_to_reconnect() -> None:
    d = decide(Observation(Screen.GAME, signal_age=60.0, screen_age=600.0), ON)
    assert (d.action, d.phase) == (Action.NONE, Phase.RECONECTANDO)


def test_dead_signal_reloads() -> None:
    d = decide(Observation(Screen.GAME, signal_age=200.0, screen_age=600.0), ON)
    assert (d.action, d.phase) == (Action.RELOAD, Phase.RECONECTANDO)


def test_remote_viewer_blocks_reload() -> None:
    obs = Observation(Screen.GAME, signal_age=200.0, screen_age=600.0)
    assert decide(obs, Context(desired_on=True, manual=True)).action is Action.NONE


def test_crash_reloads() -> None:
    d = decide(Observation(Screen.LOADING, crashed=True), ON)
    assert d.action is Action.RELOAD


def test_replaced_turns_off_even_with_remote_viewer() -> None:
    obs = Observation(Screen.BLOCK, block_kind=BlockKind.REPLACED, block_text="outro dispositivo")
    d = decide(obs, Context(desired_on=True, manual=True))
    assert (d.action, d.phase, d.off_reason) == (
        Action.TURN_OFF,
        Phase.ASSUMIDO,
        OffReason.REPLACED,
    )


def test_stop_block_turns_off_with_error() -> None:
    obs = Observation(Screen.BLOCK, block_kind=BlockKind.STOP, block_text="inatividade")
    d = decide(obs, ON)
    assert (d.action, d.off_reason) == (Action.TURN_OFF, OffReason.ERROR)
    assert d.reason == "inatividade"


def test_expired_session_reloads_never_clicks() -> None:
    d = decide(Observation(Screen.BLOCK, block_kind=BlockKind.RELOGIN), ON)
    assert d.action is Action.RELOAD


def test_maintenance_waits_then_reloads() -> None:
    early = Observation(Screen.BLOCK, block_kind=BlockKind.MAINTENANCE, screen_age=30.0)
    late = Observation(Screen.BLOCK, block_kind=BlockKind.MAINTENANCE, screen_age=900.0)
    assert decide(early, ON).action is Action.NONE
    assert decide(late, ON).action is Action.RELOAD


def test_unknown_block_waits_then_reloads() -> None:
    early = Observation(Screen.BLOCK, block_kind=BlockKind.UNKNOWN, screen_age=10.0)
    late = Observation(Screen.BLOCK, block_kind=BlockKind.UNKNOWN, screen_age=120.0)
    assert decide(early, ON).action is Action.NONE
    assert decide(late, ON).action is Action.RELOAD


def test_login_screen_logs_in() -> None:
    d = decide(Observation(Screen.LOGIN), ON)
    assert (d.action, d.phase) == (Action.LOGIN, Phase.ENTRANDO)


@pytest.mark.parametrize(
    "ctx",
    [
        Context(desired_on=True, has_credentials=False),
        Context(desired_on=True, login_latched=True),
    ],
)
def test_login_needs_owner(ctx: Context) -> None:
    d = decide(Observation(Screen.LOGIN), ctx)
    assert (d.action, d.phase) == (Action.NONE, Phase.PRECISA_LOGIN)


@pytest.mark.parametrize(
    "ctx",
    [Context(desired_on=True, manual=True), Context(desired_on=True, login_allowed=False)],
)
def test_login_waits(ctx: Context) -> None:
    d = decide(Observation(Screen.LOGIN), ctx)
    assert (d.action, d.phase) == (Action.NONE, Phase.ENTRANDO)


EXPIRED = Observation(Screen.BLOCK, block_kind=BlockKind.RELOGIN, block_text="Sessão expirada")


@pytest.mark.parametrize("obs", [EXPIRED, Observation(Screen.LOGIN)])
def test_session_lost_after_playing_turns_off(obs: Observation) -> None:
    """Uma conta, um aparelho: depois de jogar, o keeper sai em vez de entrar com a senha."""
    d = decide(obs, Context(desired_on=True, relogin=Relogin.YIELD))
    assert (d.action, d.phase, d.off_reason) == (
        Action.TURN_OFF,
        Phase.ASSUMIDO,
        OffReason.REPLACED,
    )


def test_before_playing_relogin_is_free() -> None:
    free = Context(desired_on=True, relogin=Relogin.FREE)
    assert decide(EXPIRED, free).action is Action.RELOAD
    assert decide(Observation(Screen.LOGIN), free).action is Action.LOGIN


@pytest.mark.parametrize(
    "ctx",
    [
        Context(desired_on=True, login_latched=True, relogin=Relogin.YIELD),
        Context(desired_on=True, has_credentials=False, relogin=Relogin.YIELD),
    ],
)
def test_yield_wins_over_login_problems(ctx: Context) -> None:
    assert decide(Observation(Screen.LOGIN), ctx).action is Action.TURN_OFF


def test_remote_screen_open_does_not_hold_the_keeper_in_the_account() -> None:
    """12/11/2026 15:26 UTC: o dono olhava a tela preta pela tela remota e o modo manual segurou
    o aviso de "assumiu". Perder a sessão depois de jogar faz o keeper sair mesmo assim."""
    ctx = Context(desired_on=True, manual=True, relogin=Relogin.YIELD)
    for obs in (
        Observation(Screen.LOGIN),
        EXPIRED,
        Observation(Screen.GAME, signal_age=95.0, screen_age=600.0),
    ):
        d = decide(obs, ctx)
        assert (d.action, d.off_reason) == (Action.TURN_OFF, OffReason.REPLACED)


def test_remote_screen_open_before_playing_only_watches() -> None:
    ctx = Context(desired_on=True, manual=True)
    assert decide(Observation(Screen.LOGIN), ctx).action is Action.NONE
    assert decide(EXPIRED, ctx).action is Action.NONE


def test_replaced_block_always_turns_off() -> None:
    replaced = Observation(Screen.BLOCK, block_kind=BlockKind.REPLACED)
    for relogin in Relogin:
        d = decide(replaced, Context(desired_on=True, manual=True, relogin=relogin))
        assert (d.action, d.off_reason) == (Action.TURN_OFF, OffReason.REPLACED)


LOGGED_IN_STUCK = Observation(Screen.LOGIN, token=True)


def test_login_accepted_but_stuck_waits_then_reloads() -> None:
    """12/11/2026: o jogo grava a sessão e fica na tela de acesso com "Erro desconhecido" quando a
    conta ainda estava ativa em outro lugar. Recarregar retoma pela sessão; nada de 2º login."""
    recent = Context(desired_on=True, login_recent=True)
    assert decide(LOGGED_IN_STUCK, recent).action is Action.NONE
    settled = Context(desired_on=True)
    d = decide(LOGGED_IN_STUCK, settled)
    assert (d.action, d.phase) == (Action.RELOAD, Phase.ENTRANDO)
    assert (
        decide(LOGGED_IN_STUCK, Context(desired_on=True, retry_ready=False)).action is Action.NONE
    )


def test_yield_comes_before_the_stuck_reload() -> None:
    ctx = Context(desired_on=True, relogin=Relogin.YIELD)
    assert decide(LOGGED_IN_STUCK, ctx).action is Action.TURN_OFF


def test_login_without_saved_session_types_the_password() -> None:
    d = decide(Observation(Screen.LOGIN), Context(desired_on=True))
    assert (d.action, d.reason) == (Action.LOGIN, "entrando com usuário e senha")


DEAD_GAME = Observation(Screen.GAME, signal_age=95.0, screen_age=600.0)


def test_silence_after_playing_turns_off_instead_of_reloading() -> None:
    d = decide(DEAD_GAME, Context(desired_on=True, relogin=Relogin.YIELD))
    assert (d.action, d.off_reason) == (Action.TURN_OFF, OffReason.REPLACED)
    assert decide(DEAD_GAME, Context(desired_on=True)).action is Action.RELOAD  # antes de jogar
    manual = Context(desired_on=True, manual=True, relogin=Relogin.YIELD)
    assert decide(DEAD_GAME, manual).action is Action.TURN_OFF  # mesmo com a tela remota aberta
    doubt = Observation(Screen.GAME, signal_age=45.0, screen_age=600.0)
    assert decide(doubt, Context(desired_on=True, relogin=Relogin.YIELD)).action is Action.NONE


def test_loading_too_long_reloads() -> None:
    assert decide(Observation(Screen.LOADING, screen_age=10.0), ON).action is Action.NONE
    assert decide(Observation(Screen.LOADING, screen_age=120.0), ON).action is Action.RELOAD


# --- textos dos bloqueios ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (
            "Sessão encerrada\nSua conta foi acessada em outro dispositivo. Esta sessão foi "
            "encerrada por segurança.\nEntrar novamente",
            BlockKind.REPLACED,
        ),
        ("Sessão encerrada\nSessão encerrada por inatividade.\nEntrar novamente", BlockKind.STOP),
        ("Sessão encerrada\nVocê foi desconectado pelo servidor.", BlockKind.STOP),
        (
            "Limite de 3 contas no jogo ao mesmo tempo por IP. Burlar essa regra pode levar ao "
            "banimento da sua conta.",
            BlockKind.STOP,
        ),
        ("Sessão expirada. Entre novamente.", BlockKind.RELOGIN),
        (
            "SERVIDOR EM MANUTENÇÃO\nEstamos realizando uma manutenção administrativa.",
            BlockKind.MAINTENANCE,
        ),
        ("Falha no carregamento\nTentar novamente", BlockKind.UNKNOWN),
    ],
)
def test_classify_default_texts(text: str, kind: BlockKind) -> None:
    assert BlockMatcher.from_texts({}).classify(text) is kind


def test_classify_uses_current_game_texts() -> None:
    files = [{"errors": {"session": {"idle": "Você ficou parado demais."}}}]
    matcher = BlockMatcher.from_texts(locale_texts(files))
    assert matcher.classify("Sessão encerrada\nVocê ficou parado demais.") is BlockKind.STOP
    # O texto antigo deixa de valer para essa chave.
    assert matcher.classify("Sessão encerrada por inatividade.") is BlockKind.UNKNOWN
