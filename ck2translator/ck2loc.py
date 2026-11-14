"""ck2loc.py - leitura e escrita fiel de arquivos de localisation (.csv) do CK2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .files import atomic_write

# Layout padrão do CK2:  #CODE;ENGLISH;FRENCH;GERMAN;;SPANISH;;;;;;;;;x
# Se o arquivo tiver um cabeçalho "#CODE;..." os índices são lidos dele.
DEFAULT_COLUMNS = {"english": 1, "french": 2, "german": 3, "spanish": 5}

# Códigos do jogo que NUNCA podem ser traduzidos nem alterados.
TOKEN_RE = re.compile(
    r"\$[^$\s;]+\$"                  # $VARIAVEL$
    r"|\[[^\[\];]+\]"                # [Root.GetName]  [This.Custom('x')]
    r"|§[A-Za-z!]"              # §Y  §!   (cores)
    r"|£[^£\s;]+£"    # £gold£   (ícones)
    r"|\\[nt]"                       # \n  \t  literais
    r"|\{[^{}]*\}"                   # {qualquer coisa} usada por alguns mods
)

# O EU4 também usa bandeiras @TAG / @[Root.GetTag] e escapes de aspas e
# barras. O padrão CK2 fica intacto para preservar o cache e os CSV legados.
EU4_TOKEN_RE = re.compile(
    TOKEN_RE.pattern
    + r"|@(?:[A-Z0-9]{3}(?![A-Za-z0-9_])|\[[^\[\]\r\n]+\])"
    + r"|\\[^\r\n]"
)

# Caracteres que o jogo interpreta. A QUANTIDADE de cada um não pode mudar na tradução.
STRUCT_CHARS = "[]$\u00a7\u00a3{}\\"


def hard_problems(src: str, dst: str) -> list[str]:
    """Diferenças que NUNCA são aceitáveis entre o texto original e o traduzido (lista vazia = seguro)."""
    p: list[str] = []
    for ch in STRUCT_CHARS:
        if src.count(ch) != dst.count(ch):
            p.append(f"quantidade de '{ch}' mudou ({src.count(ch)} -> {dst.count(ch)})")
    if sorted(TOKEN_RE.findall(src)) != sorted(TOKEN_RE.findall(dst)):
        p.append("códigos do jogo diferentes")
    if (src[:len(src) - len(src.lstrip())] != dst[:len(dst) - len(dst.lstrip())]
            or src[len(src.rstrip()):] != dst[len(dst.rstrip()):]):
        p.append("espaços no início/fim mudaram")
    if any(ord(c) < 32 for c in dst):
        p.append("caracteres de controle no texto")
    if ";" in dst:
        p.append("ponto e vírgula no texto")
    return p


# --------------------------------------------------------------------------- bytes <-> texto
def decode_bytes(data: bytes) -> tuple[str, str]:
    """Devolve (texto, encoding). O encoding é um de: cp1252, utf-8, utf-8-sig."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="surrogateescape"), "utf-8-sig"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="surrogateescape"), "cp1252"
    # Só ASCII: tanto faz, tratamos como cp1252 (padrão do CK2).
    return text, ("utf-8" if any(b >= 0x80 for b in data) else "cp1252")


def encode_text(text: str, encoding: str) -> bytes:
    codec = "utf-8" if encoding.startswith("utf-8") else "cp1252"
    raw = text.encode(codec, errors="surrogateescape")
    return b"\xef\xbb\xbf" + raw if encoding == "utf-8-sig" else raw


# --------------------------------------------------------------------------- linhas
@dataclass
class Line:
    raw: str                         # conteúdo original, sem o fim de linha
    eol: str                         # "\r\n", "\n" ou "" (última linha sem quebra)
    fields: list[str] | None = None  # None => comentário / linha vazia
    dirty: bool = False

    @property
    def is_entry(self) -> bool:
        return self.fields is not None

    @property
    def key(self) -> str:
        return self.fields[0].strip() if self.fields else ""

    def text(self) -> str:
        # Linha não modificada: devolve exatamente o original.
        return ";".join(self.fields) if self.dirty and self.fields else self.raw


def _parse_line(raw: str, eol: str) -> Line:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#") or ";" not in raw:
        return Line(raw, eol)
    return Line(raw, eol, fields=raw.split(";"))


def split_lines(text: str) -> list[Line]:
    # Não usamos str.splitlines(): ele quebra em caracteres que o CK2 não trata como quebra.
    parts = text.split("\n")
    lines: list[Line] = []
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        if is_last and part == "":
            break                                  # o texto terminava com "\n"
        eol = "" if is_last else "\n"
        if eol and part.endswith("\r"):
            part, eol = part[:-1], "\r\n"
        lines.append(_parse_line(part, eol))
    return lines


def detect_columns(lines: list[Line]) -> dict[str, int]:
    cols = dict(DEFAULT_COLUMNS)
    for ln in lines:
        s = ln.raw.lstrip()
        if s.startswith("#") and ";" in s:
            names = [p.strip().lower() for p in s.lstrip("#").split(";")]
            if names and names[0] == "code":
                cols.update({n: i for i, n in enumerate(names) if n in DEFAULT_COLUMNS})
                break
    return cols


# --------------------------------------------------------------------------- arquivo
@dataclass
class LocFile:
    path: Path
    encoding: str
    lines: list[Line]
    columns: dict[str, int]

    @classmethod
    def read(cls, path: str | Path) -> "LocFile":
        path = Path(path)
        text, enc = decode_bytes(path.read_bytes())
        lines = split_lines(text)
        return cls(path, enc, lines, detect_columns(lines))

    def entries(self) -> Iterator[Line]:
        return (ln for ln in self.lines if ln.is_entry)

    def set_cell(self, line: Line, col: int, value: str) -> None:
        if not line.is_entry:
            raise ValueError("a linha não é uma entrada de localisation")
        if ";" in value or "\n" in value or "\r" in value:
            raise ValueError(f"valor inválido para a chave {line.key}: contém ';' ou quebra de linha")
        if not self.encoding.startswith("utf-8"):
            value.encode("cp1252")                 # levanta UnicodeEncodeError se não couber
        if not 0 <= col < len(line.fields):
            raise ValueError(f"coluna {col} ausente na chave {line.key}; a estrutura do CSV será preservada")
        if col == len(line.fields) - 1 and line.fields[col].strip().lower() == "x" and value != line.fields[col]:
            raise ValueError(f"a coluna {col} da chave {line.key} contém o marcador final x")
        line.fields[col] = value
        line.dirty = True

    def to_bytes(self) -> bytes:
        return encode_text("".join(ln.text() + ln.eol for ln in self.lines), self.encoding)

    def write(self, path: str | Path) -> None:
        path = Path(path)
        atomic_write(path, self.to_bytes())


# --------------------------------------------------------------------------- verificação
def compare(src: LocFile, dst: LocFile, target_cols: set[int], source_col: int) -> list[str]:
    """Compara original e traduzido. Só as colunas-alvo podem mudar, e o que for gravado
    nelas deve ter os mesmos códigos do jogo ($X$, [..], §Y, £x£, \\n) do texto da coluna-fonte."""
    problems: list[str] = []
    if src.encoding != dst.encoding:
        problems.append(f"encoding mudou: {src.encoding} -> {dst.encoding}")
    if len(src.lines) != len(dst.lines):
        return problems + [f"número de linhas difere: {len(src.lines)} vs {len(dst.lines)}"]
    for n, (a, b) in enumerate(zip(src.lines, dst.lines), start=1):
        if a.eol != b.eol:
            problems.append(f"linha {n}: fim de linha diferente")
        if a.is_entry != b.is_entry:
            problems.append(f"linha {n}: deixou de ser/passou a ser uma entrada")
            continue
        if not a.is_entry:
            if a.text() != b.text():
                problems.append(f"linha {n}: comentário/linha vazia alterado")
            continue
        if len(a.fields) != len(b.fields):
            problems.append(f"linha {n} ({a.key}): número de colunas difere")
            continue
        if a.fields[-1].strip().lower() == "x" and a.fields[-1] != b.fields[-1]:
            problems.append(f"linha {n} ({a.key}): marcador final x foi alterado")
        for col, (fa, fb) in enumerate(zip(a.fields, b.fields)):
            if col in target_cols:
                if fa == fb:
                    continue                     # linha pulada (vazia, só código ou falha): intacta
                origem = a.fields[source_col] if source_col < len(a.fields) else ""
                for msg in hard_problems(origem, fb):
                    problems.append(f"linha {n} ({a.key}): {msg} (coluna {col})")
            elif fa != fb:
                problems.append(f"linha {n} ({a.key}): coluna {col} foi alterada")
    return problems
