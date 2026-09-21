"""Gera web/icone.ico a partir do codigo -- sem binario opaco no repositorio.

    python make_icon.py

Desenha um anel partido em tres arcos, um por IA (Claude / Codex / Antigravity),
sobre um quadrado escuro de cantos arredondados. E' a mesma ideia da marca no
topo da interface.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

FUNDO = (14, 15, 18, 255)
CORES = [
    (217, 119, 87, 255),   # claude      -- terracota
    (70, 207, 160, 255),   # codex       -- verde-menta
    (139, 123, 247, 255),  # antigravity -- azul-violeta
]
TAMANHOS = [256, 128, 64, 48, 32, 16]
SUPER = 8  # superamostragem: desenha grande e reduz, para a borda sair lisa


def desenhar(lado):
    px = lado * SUPER
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    raio = int(px * 0.22)
    d.rounded_rectangle([0, 0, px - 1, px - 1], radius=raio, fill=FUNDO)

    margem = px * 0.24
    caixa = [margem, margem, px - margem, px - margem]
    espessura = max(1, int(px * 0.115))

    # Tres arcos de 104 graus com 16 graus de folga entre eles.
    for i, cor in enumerate(CORES):
        inicio = -90 + i * 120 + 8
        d.arc(caixa, start=inicio, end=inicio + 104, fill=cor, width=espessura)

    # Ponto central: o "ao vivo" do radar.
    r = px * 0.055
    meio = px / 2
    d.ellipse([meio - r, meio - r, meio + r, meio + r], fill=CORES[1])

    return img.resize((lado, lado), Image.LANCZOS)


def main():
    destino = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "icone.ico")
    imagens = [desenhar(t) for t in TAMANHOS]
    imagens[0].save(destino, format="ICO", sizes=[(t, t) for t in TAMANHOS])
    png = destino.replace(".ico", ".png")
    imagens[0].save(png, format="PNG")
    print("gerado: %s (%d bytes)" % (destino, os.path.getsize(destino)))
    print("gerado: %s" % png)


if __name__ == "__main__":
    main()
