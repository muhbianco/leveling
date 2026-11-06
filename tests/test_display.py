from __future__ import annotations

from app.config import Settings
from app.display import Display


def test_vnc_server_is_tigervnc_on_loopback_with_frame_cap() -> None:
    settings = Settings(manage_display=False, vnc_frame_rate=30)
    commands = Display(settings, ":100", 5901)._commands()
    assert commands["Xvfb"] == ["Xvfb", ":100", "-screen", "0", "1280x720x24", "-nolisten", "tcp"]
    vnc = commands["vnc"]
    assert vnc[0] == "X0tigervnc"
    assert vnc[1:5] == ["-display", ":100", "-rfbport", "5901"]
    # Só a ponte autenticada chega nele; sem senha de VNC; quadros com teto.
    assert {"-localhost=1", "-SecurityTypes=None", "-AlwaysShared=1", "-FrameRate=30"} <= set(vnc)
    assert "-MaxProcessorUsage=100" in vnc
    assert not any(a.startswith("-noxdamage") for a in vnc)
