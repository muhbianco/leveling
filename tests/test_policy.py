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
@pytest.mark.parametrize(
    ("relogin", "action"),
    [(Relogin.ASK, Action.HOLD), (Relogin.WAIT, Action.NONE)],
)
def test_relogin_after_playing_waits_for_owner(
    obs: Observation, relogin: Relogin, action: Action
) -> None:
    d = decide(obs, Context(desired_on=True, relogin=relogin))
    assert (d.action, d.phase) == (action, Phase.CONFIRMANDO)


def test_relogin_window_over_goes_ahead() -> None:
    free = Context(desired_on=True, relogin=Relogin.FREE)
    assert decide(EXPIRED, free).action is Action.RELOAD
    assert decide(Observation(Screen.LOGIN), free).action is Action.LOGIN


@pytest.mark.parametrize(
    ("ctx", "phase"),
    [
        (Context(desired_on=True, manual=True, relogin=Relogin.ASK), Phase.ENTRANDO),
        (Context(desired_on=True, login_latched=True, relogin=Relogin.ASK), Phase.PRECISA_LOGIN),
        (Context(desired_on=True, has_credentials=False, relogin=Relogin.ASK), Phase.PRECISA_LOGIN),
    ],
)
def test_no_window_when_nothing_would_log_in(ctx: Context, phase: Phase) -> None:
    d = decide(Observation(Screen.LOGIN), ctx)
    assert (d.action, d.phase) == (Action.NONE, phase)


def test_window_never_hides_the_replaced_block() -> None:
    replaced = Observation(Screen.BLOCK, block_kind=BlockKind.REPLACED)
    d = decide(replaced, Context(desired_on=True, relogin=Relogin.ASK))
    assert d.action is Action.TURN_OFF


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
