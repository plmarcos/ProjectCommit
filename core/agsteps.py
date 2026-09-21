"""A conversa do Antigravity, decodificada.

Ate' aqui o painel dizia "o Antigravity guarda a conversa em protobuf sem schema
publico, entao o texto nao e' legivel" -- e isso estava errado. O wire format e'
auto-descritivo (ver core/pbwalk.py); o que faltava era mapear onde cada coisa
mora dentro de `conversations/<id>.db`, tabela `steps`.

Medido nas 12 conversas deste PC (1.977 passos):

    step_type  14   fala do usuario          19.2 / 19.3.1
    step_type  15   passo do modelo          20.1 / 20.8  resposta
                                             20.3         raciocinio
                                             20.7.3       chamada de ferramenta (JSON)
    step_type  23   cabecalho da conversa    30.4   TITULO   (12 de 12)
                                             30.19  primeiro prompt
    step_type 132   resultado de ferramenta  140.1.2 / 5.4.3
    qualquer um     data em segundos         5.1.1

Os caminhos sao PREFERENCIA, nunca exigencia: se o Google renumerar os campos, a
escolha cai para "a maior frase perto da raiz do passo" e, se nem isso houver, o
passo e' ignorado. A varredura nao pode cair por causa de um formato que muda.

O `30.4` resolve sozinho um defeito visivel: 9 das 12 conversas apareciam sem
nome, porque `annotations/<id>.pbtxt` so' traz `last_user_view_time`. O titulo
sempre esteve no `.db`, num campo que ninguem lia.
"""
from __future__ import annotations

import json

from . import pbwalk

USUARIO = 14
MODELO = 15
CABECALHO = 23
FERRAMENTA = 132

#: Os passos que viram mensagem. Os demais tipos (90, 21, 101, 8, 5, 9...) sao
#: contabilidade interna da trajetoria e nao carregam fala.
FALANTES = (USUARIO, MODELO, FERRAMENTA)

_CAMINHO_DATA = "5.1.1"
_CHAVES_CWD = ("Cwd", "cwd", "Workdir", "workdir", "WorkingDirectory")


def ler(step_type, payload):
    """Um passo -> {papel, texto, ts, ferramentas, cwds} ou None.

    Papeis no mesmo vocabulario do leitor de conversa (core/transcript.py), para
    que o filtro de papeis da interface funcione igual para as tres IAs.
    """
    if step_type not in FALANTES or not payload:
        return None
    textos, carimbos = pbwalk.esquadrinhar(payload)
    if not textos:
        return None
    ts = pbwalk.carimbo(carimbos, _CAMINHO_DATA)

    if step_type == USUARIO:
        texto = pbwalk.por_caminho(textos, "19.2", "19.3.1") or pbwalk.maior_texto(textos)
        if not texto:
            return None
        papel = "contexto" if texto.lstrip().startswith("<") else "user"
        return {"papel": papel, "texto": texto.strip(), "ts": ts,
                "ferramentas": [], "cwds": []}

    if step_type == MODELO:
        resposta = pbwalk.por_caminho(textos, "20.1", "20.8")
        pensou = pbwalk.por_caminho(textos, "20.3")
        chamadas = [t for c, t in textos if c.startswith("20.7") and t.lstrip().startswith("{")]
        partes = []
        if resposta:
            partes.append(resposta.strip())
        elif pensou:
            # Mesmo tratamento que o leitor ja' da' ao raciocinio do Claude.
            partes.append("∴ " + pensou.strip())
        nomes, cwds = _das_chamadas(chamadas)
        if not partes and not nomes:
            texto = pbwalk.maior_texto(textos)
            if not texto:
                return None
            partes.append(texto.strip())
        return {"papel": "assistant", "texto": "\n\n".join(partes), "ts": ts,
                "ferramentas": nomes, "cwds": cwds}

    texto = pbwalk.por_caminho(textos, "140.1.2", "5.4.3") or pbwalk.maior_texto(textos)
    if not texto:
        return None
    return {"papel": "ferramenta", "texto": texto.strip(), "ts": ts,
            "ferramentas": [], "cwds": []}


def _das_chamadas(chamadas):
    """Nomes de ferramenta e diretorios de trabalho declarados nas chamadas.

    O `Cwd` daqui vale ouro: e' o unico lugar em que o Antigravity DECLARA o
    diretorio, contra a heuristica de frequencia de strings que era tudo o que
    havia antes para descobrir o projeto.
    """
    nomes, cwds = [], []
    for bruto in chamadas:
        try:
            dados = json.loads(bruto)
        except ValueError:
            continue
        if not isinstance(dados, dict):
            continue
        rotulo = dados.get("toolSummary") or dados.get("toolAction")
        if rotulo:
            nomes.append(str(rotulo))
        for chave in _CHAVES_CWD:
            valor = dados.get(chave)
            if isinstance(valor, str) and len(valor) > 3:
                cwds.append(valor)
                break
    return nomes, cwds


def cabecalho(payload):
    """(titulo, primeiro_prompt) do passo de cabecalho. (None, None) se nao der."""
    if not payload:
        return None, None
    textos = pbwalk.walk(payload)
    titulo = pbwalk.por_caminho(textos, "30.4")
    primeiro = pbwalk.por_caminho(textos, "30.19", "30.18.3")
    if titulo:
        titulo = titulo.strip() or None
    return titulo, (primeiro.strip() if primeiro else None)


def percorrer(ro, limite_passos=6000):
    """Le a tabela `steps` de uma conversa aberta em modo leitura.

    Devolve (cabecalho, mensagens) onde cabecalho e' (titulo, primeiro_prompt) e
    mensagens e' [(idx, dict de ler())]. Nunca levanta por passo estragado.
    """
    titulo = primeiro = None
    mensagens = []
    try:
        linhas = ro.execute(
            "SELECT idx, step_type, step_payload FROM steps ORDER BY idx LIMIT ?",
            (limite_passos,),
        )
    except Exception:
        return (None, None), []

    for idx, tipo, payload in linhas:
        try:
            if tipo == CABECALHO:
                t, p = cabecalho(payload)
                titulo = titulo or t
                primeiro = primeiro or p
                continue
            passo = ler(tipo, payload)
        except Exception:
            continue          # um passo ilegivel nao derruba a conversa
        if passo:
            mensagens.append((idx, passo))
    return (titulo, primeiro), mensagens
