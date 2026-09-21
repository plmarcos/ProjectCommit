"""Leitor de protobuf SEM schema.

O Antigravity guarda a conversa em blobs protobuf e o Google nao publica os
.proto. Por muito tempo isto passou por "ilegivel" -- mas o wire format e'
auto-descritivo o bastante para ser percorrido sem schema nenhum: cada campo
comeca por um varint que carrega o NUMERO do campo e o TIPO de codificacao.

    chave = (numero << 3) | tipo
    tipo 0  varint          -- inteiro, pulamos
    tipo 1  64 bits         -- pulamos
    tipo 2  delimitado      -- bytes: ou e' texto, ou e' outra mensagem
    tipo 5  32 bits         -- pulamos

O unico palpite e' no tipo 2: tentamos decodificar como UTF-8 legivel; se nao
der, tratamos como submensagem e descemos. E' o suficiente para tirar texto,
titulo e chamadas de ferramenta de dentro dos blobs.

    >>> walk(blob)
    [("2", "gemini, analize o conteudo desse video"), ("20.7.3", '{"CommandLine":...}')]

O caminho devolvido ("20.7.3") e' a trilha de numeros de campo ate' aquele
texto. Ele serve como PREFERENCIA de quem chama, nunca como exigencia: se o
Google renumerar os campos, quem chama cai para a heuristica de forma e o
resultado piora -- nunca quebra.
"""
from __future__ import annotations

#: Acima disto um campo delimitado nao e' texto de conversa; nem tentamos
#: decodificar (poupa gastar UTF-8 em cima de JPEG embutido).
_MAX_TEXTO = 256 * 1024

#: Teto de strings por blob. Um payload com imagem pode gerar milhares de
#: fragmentos falsos ao ser interpretado como submensagem.
_MAX_ITENS = 600

_BRANCOS = "\n\r\t"

#: Um varint dentro desta faixa e' data em SEGUNDOS, nao contador. Guardamos
#: so' esses -- um blob tem milhares de inteiros e nenhum outro interessa.
#: A faixa cobre 2020-2036, que e' o horizonte util aqui.
_EPOCH_MIN, _EPOCH_MAX = 1_580_000_000, 2_100_000_000


def _varint(buf, i):
    """Le um varint a partir de buf[i]. Devolve (valor, proximo indice)."""
    resultado = deslocamento = 0
    fim = len(buf)
    while i < fim:
        byte = buf[i]
        i += 1
        resultado |= (byte & 0x7F) << deslocamento
        if not byte & 0x80:
            return resultado, i
        deslocamento += 7
        if deslocamento > 63:
            raise ValueError("varint longo demais")
    raise ValueError("varint truncado")


def _legivel(texto):
    """Texto de gente: imprimivel, com quebra de linha e tabulacao liberadas."""
    if not texto:
        return False
    return all(c.isprintable() or c in _BRANCOS for c in texto)


def esquadrinhar(blob, max_depth=8):
    """Uma passada, dois resultados: (textos, carimbos).

    `textos`   [(caminho, str)]  -- campos delimitados que sao texto legivel
    `carimbos` [(caminho, int)]  -- varints que caem na faixa de data

    Nunca levanta: blob corrompido ou de outro formato devolve o que deu para
    ler ate' o ponto em que a estrutura deixou de fazer sentido.
    """
    textos, carimbos = [], []
    if blob:
        _walk(blob, 0, "", textos, carimbos, max_depth)
    return textos, carimbos


def walk(blob, max_depth=8):
    """So' os textos -- atalho para quem nao precisa das datas."""
    return esquadrinhar(blob, max_depth)[0]


def _walk(buf, depth, trilha, saida, carimbos, max_depth):
    i, fim = 0, len(buf)
    while i < fim:
        if len(saida) >= _MAX_ITENS:
            return
        try:
            chave, i = _varint(buf, i)
        except ValueError:
            return
        numero, tipo = chave >> 3, chave & 7
        if numero == 0:
            return  # campo 0 nao existe: nao era protobuf

        if tipo == 0:
            try:
                valor, i = _varint(buf, i)
            except ValueError:
                return
            if _EPOCH_MIN <= valor <= _EPOCH_MAX:
                carimbos.append(("%s.%d" % (trilha, numero) if trilha else str(numero), valor))
        elif tipo == 5:
            i += 4
        elif tipo == 1:
            i += 8
        elif tipo == 2:
            try:
                tamanho, i = _varint(buf, i)
            except ValueError:
                return
            if tamanho < 0 or i + tamanho > fim:
                return
            pedaco = buf[i:i + tamanho]
            i += tamanho
            caminho = "%s.%d" % (trilha, numero) if trilha else str(numero)

            # Ambiguidade central do formato: um campo delimitado pode ser texto
            # OU outra mensagem, e o wire format nao diz qual. Uma submensagem
            # cujo unico campo e' texto decodifica como UTF-8 valida -- foi assim
            # que "gemini, analize..." saiu como "Wgemini, analize...", com o
            # byte de comprimento colado na frente. Por isso a estrutura tem
            # precedencia: so' fica como texto o que NAO se explica melhor como
            # mensagem, ou cuja descida nao rendeu nada.
            texto = None
            if tamanho <= _MAX_TEXTO:
                try:
                    candidato = pedaco.decode("utf-8")
                except UnicodeDecodeError:
                    candidato = None
                if candidato is not None and _legivel(candidato):
                    texto = candidato

            if depth < max_depth and _parece_mensagem(pedaco):
                antes = (len(saida), len(carimbos))
                _walk(pedaco, depth + 1, caminho, saida, carimbos, max_depth)
                if (len(saida), len(carimbos)) != antes:
                    continue
            if texto is not None:
                saida.append((caminho, texto))
        else:
            return  # tipos 3, 4 e 6 nao existem no wire format atual


def _parece_mensagem(buf):
    """Varre a estrutura sem decodificar nada: consome o buffer INTEIRO?

    Barato de proposito -- roda antes de cada descida. Texto de gente quase
    sempre falha logo no segundo campo (um tipo de wire invalido basta).
    """
    if not buf:
        return False
    i, fim, campos = 0, len(buf), 0
    while i < fim:
        try:
            chave, i = _varint(buf, i)
        except ValueError:
            return False
        numero, tipo = chave >> 3, chave & 7
        if numero == 0:
            return False
        if tipo == 0:
            try:
                _, i = _varint(buf, i)
            except ValueError:
                return False
        elif tipo == 1:
            i += 8
        elif tipo == 5:
            i += 4
        elif tipo == 2:
            try:
                tamanho, i = _varint(buf, i)
            except ValueError:
                return False
            if tamanho < 0 or i + tamanho > fim:
                return False
            i += tamanho
        else:
            return False
        campos += 1
        if i > fim:
            return False
    return campos > 0


def por_caminho(itens, *caminhos):
    """Primeiro texto cujo caminho bate exatamente com um dos pedidos."""
    for alvo in caminhos:
        for caminho, texto in itens:
            if caminho == alvo:
                return texto
    return None


def carimbo(carimbos, *caminhos):
    """Data do passo em segundos, ou None.

    Prefere os caminhos pedidos; se nenhum bater, pega o MENOR de todos -- num
    passo do modelo ha' varias datas (inicio, fim de cada etapa) e a primeira e'
    a que situa o passo na linha do tempo.
    """
    for alvo in caminhos:
        for caminho, valor in carimbos:
            if caminho == alvo:
                return float(valor)
    if not carimbos:
        return None
    return float(min(v for _c, v in carimbos))


def maior_texto(itens, minimo=12, max_profundidade=4, ignorar=()):
    """A maior string que parece frase, para quando o caminho conhecido falha.

    Descarta o que e' identificador (UUID, `bot-...`, chave opaca) e o que esta'
    fundo demais: a mensagem de gente fica perto da raiz do passo, enquanto o
    contexto que a ferramenta injeta -- lista de plugins, catalogo de skills --
    mora em ramos como 19.12.1.46.5.1. Sem o limite de profundidade a heuristica
    escolhia justamente esse entulho.
    """
    melhor = None
    for caminho, texto in itens:
        t = texto.strip()
        if len(t) < minimo or caminho in ignorar:
            continue
        if caminho.count(".") + 1 > max_profundidade:
            continue
        if _identificador(t):
            continue
        if melhor is None or len(t) > len(melhor):
            melhor = t
    return melhor


def _identificador(texto):
    """Cheira a chave e nao a frase."""
    if " " in texto.strip():
        return False  # tem espaco: e' frase
    corpo = texto.strip().strip("-_")
    if not corpo:
        return True
    if corpo.startswith(("bot-", "sessionID", "urn:", "file:///")):
        return True
    # UUID e afins: so' hexadecimal, tracos e digitos, sem vogal de palavra.
    hexa = sum(1 for c in corpo if c in "0123456789abcdefABCDEF-")
    return hexa == len(corpo) and len(corpo) >= 16
