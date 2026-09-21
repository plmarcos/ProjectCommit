"""ProjectCommit -- painel unificado de projetos e IAs.

Sobe o Flask numa porta efemera de 127.0.0.1, em thread, e abre uma janela
pywebview apontada para ela. No Windows 11 o motor e' o WebView2 (ja' presente
no sistema), entao a janela e' leve -- nao ha' Chromium embutido no pacote.

    python app.py            abre a janela
    python app.py --server   so' o servidor, para depurar no navegador
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import logging
import os
import socket
import sys
import threading
import time
from wsgiref.simple_server import WSGIRequestHandler, make_server

from core import api, db, settings

APP_TITLE = "ProjectCommit"


class QuietHandler(WSGIRequestHandler):
    """Sem log de request por linha -- polui o console e nao ajuda em nada."""

    def log_message(self, *args):
        pass


def enable_dpi_awareness():
    """Declara consciencia de DPI por monitor ANTES de criar a janela.

    Sem isto, num monitor a 150% (o caso deste PC) o Windows liga a virtualizacao
    de DPI: a moldura da janela e o filho WebView2 passam a trabalhar em escalas
    diferentes e o conteudo aparece cortado, pintando so' uma faixa a' esquerda.
    """
    if os.name != "nt":
        return
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703+)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


#: Tamanho ideal da janela em pixels LOGICOS (= pixels CSS dentro do WebView2).
IDEAL_W, IDEAL_H = 1360, 880
MIN_W, MIN_H = 900, 600


def window_geometry():
    """Tamanho E posicao da janela, em pixels LOGICOS, dentro da area util.

    Medido em vez de suposto: uma sonda mostrou que o pywebview interpreta
    width/height em pixels logicos (CSS), nao fisicos. Neste monitor a 150% a
    area util tem 1920x1040 FISICOS, que sao apenas 1280x693 logicos -- pedir
    1360 fazia a janela nascer maior que a tela.

    A posicao vai junto de proposito: sem passar x/y a janela reaproveitava a
    colocacao anterior e nascia com a borda esquerda fora da tela, escondendo a
    barra lateral. Aqui ela e' centrada na area util e presa dentro dela.
    """
    fallback = {"width": IDEAL_W, "height": IDEAL_H, "min_size": (MIN_W, MIN_H)}
    if os.name != "nt":
        return fallback
    try:
        rect = ctypes.wintypes.RECT()
        # SPI_GETWORKAREA = 0x0030: area util em pixels fisicos, sem a barra de tarefas.
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        phys_w, phys_h = rect.right - rect.left, rect.bottom - rect.top
        scale = max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except (AttributeError, OSError, ValueError):
        return fallback
    if phys_w < 400 or phys_h < 300:
        return fallback

    logic_w, logic_h = phys_w / scale, phys_h / scale
    left, top = rect.left / scale, rect.top / scale

    width = int(min(IDEAL_W, logic_w * 0.94))
    height = int(min(IDEAL_H, logic_h * 0.94))
    return {
        "width": width,
        "height": height,
        "x": int(max(left, left + (logic_w - width) / 2)),
        "y": int(max(top, top + (logic_h - height) / 2)),
        "min_size": (min(MIN_W, width), min(MIN_H, height)),
    }


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port):
    httpd = make_server("127.0.0.1", port, app, handler_class=QuietHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def wait_ready(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.4):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--server", action="store_true", help="so' o servidor, sem janela")
    ap.add_argument("--port", type=int, default=0, help="porta fixa (padrao: efemera)")
    ap.add_argument("--no-scan", action="store_true", help="nao varrer ao abrir")
    ap.add_argument("--debug", action="store_true", help="abre o devtools na janela")
    ap.add_argument("--rota", default="", help="tela inicial: radar, timeline, consumo, faxina...")
    args = ap.parse_args(argv)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    enable_dpi_awareness()

    con = db.init(db.connect())
    con.close()

    flask_app, job = api.create_app()
    port = args.port or free_port()
    serve(flask_app, port)
    url = "http://127.0.0.1:%d/" % port
    if args.rota:
        url += "#/" + args.rota.lstrip("#/")

    if not wait_ready(port):
        print("servidor nao respondeu em 10s", file=sys.stderr)
        return 1

    if not args.no_scan:
        job.start()  # varredura incremental em segundo plano ao abrir

    if args.server:
        print("%s servindo em %s  (Ctrl+C encerra)" % (APP_TITLE, url))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    import webview

    webview.create_window(
        APP_TITLE, url,
        background_color="#0E0F12",
        text_select=True,
        **window_geometry(),
    )
    webview.start(debug=args.debug, private_mode=False, storage_path=settings.data_dir())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
