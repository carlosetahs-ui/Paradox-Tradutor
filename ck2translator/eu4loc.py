"""Leitura e escrita fiel do dialecto de localisation .yml do EU4.

Não é YAML genérico: versões depois de ``:`` e aspas internas de mods são
aceitas. Valores mantêm seus escapes literais; somente seu conteúdo é editado.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .ck2loc import EU4_TOKEN_RE, STRUCT_CHARS, color_problems
from .files import atomic_write

_HEADER_RE = re.compile(r"[ \t]*l_([A-Za-z0-9_]+):[ \t]*(?:#.*)?\Z")
_ENTRY_RE = re.compile(r'(?P<prefix>[ \t]*(?P<key>[^\s:#"]+):[ \t]*[0-9]*[ \t]*")')
_SUFFIX_RE = re.compile(r"[ \t]*(?:#.*)?\Z")


@dataclass
class Line:
    raw: str
    eol: str
    language: str | None = None
    key: str = ""
    value: str | None = None
    prefix: str = ""
    suffix: str = ""
    dirty: bool = False
    invalid_reason: str | None = None

    @property
    def is_entry(self) -> bool:
        return self.value is not None

    def text(self) -> str:
        if self.dirty and self.is_entry:
            return self.prefix + self.value + self.suffix
        return self.raw


def _parse_entry(raw: str, eol: str, language: str | None) -> Line:
    match = _ENTRY_RE.match(raw)
    if match is None:
        raise ValueError('entrada inválida: esperado chave:0 "texto"')
    start = match.end()
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"' and _SUFFIX_RE.fullmatch(raw[index + 1:]):
            return Line(raw, eol, language, match["key"], raw[start:index],
                        match["prefix"], raw[index:])
    raise ValueError("entrada inválida: aspas finais ausentes ou conteúdo após o valor")


def split_lines(text: str, *, ignore_invalid: bool = False) -> list[Line]:
    """Preserva LF/CRLF e, no modo tolerante, linhas inválidas sem traduzi-las."""
    parts = text.split("\n")
    lines: list[Line] = []
    language = None
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if last and not part:
            break
        eol = "" if last else "\n"
        if eol and part.endswith("\r"):
            part, eol = part[:-1], "\r\n"
        stripped = part.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(Line(part, eol, language))
            continue
        header = _HEADER_RE.fullmatch(part)
        if header:
            language = header[1]
            lines.append(Line(part, eol, language))
            continue
        try:
            line = _parse_entry(part, eol, language)
            if language is None:
                raise ValueError("entrada antes do cabeçalho l_<idioma>:")
        except ValueError as error:
            if not ignore_invalid:
                raise ValueError(f"linha {index + 1}: {error}") from error
            line = Line(part, eol, language, invalid_reason=str(error))
        lines.append(line)
    return lines


def hard_problems(src: str, dst: str) -> list[str]:
    """Valida códigos/escapes sem aplicar a restrição CSV de ponto e vírgula."""
    problems = []
    for char in STRUCT_CHARS:
        if src.count(char) != dst.count(char):
            problems.append(f"quantidade de '{char}' mudou ({src.count(char)} -> {dst.count(char)})")
    if sorted(EU4_TOKEN_RE.findall(src)) != sorted(EU4_TOKEN_RE.findall(dst)):
        problems.append("códigos do jogo ou escapes diferentes")
    problems.extend(color_problems(src, dst, EU4_TOKEN_RE))
    if (src[:len(src) - len(src.lstrip())] != dst[:len(dst) - len(dst.lstrip())]
            or src[len(src.rstrip()):] != dst[len(dst.rstrip()):]):
        problems.append("espaços no início/fim mudaram")
    if any(ord(char) < 32 for char in dst):
        problems.append("caracteres de controle no texto")
    return problems


@dataclass
class LocFile:
    path: Path
    encoding: str
    lines: list[Line]

    @classmethod
    def read(cls, path: str | Path, *, ignore_invalid: bool = False) -> "LocFile":
        path = Path(path)
        data = path.read_bytes()
        # Uma fonte UTF-8 sem BOM é aceita, mas nunca se tenta CP1252 para EU4.
        text = data.decode("utf-8-sig")
        try:
            lines = split_lines(text, ignore_invalid=ignore_invalid)
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error
        encoding = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
        return cls(path, encoding, lines)

    def entries(self) -> Iterator[Line]:
        return (line for line in self.lines if line.is_entry)

    def set_value(self, line: Line, value: str) -> None:
        if not line.is_entry:
            raise ValueError("a linha não é uma entrada de localisation")
        if any(ord(char) < 32 for char in value):
            raise ValueError(f"valor inválido para {line.key}: caracteres de controle")
        value.encode("utf-8")
        # Aspas internas são permitidas, mas não podem transformar parte do
        # valor em comentário nem consumir as aspas que fecham a entrada.
        candidate = _parse_entry(line.prefix + value + line.suffix, line.eol, line.language)
        if (candidate.value, candidate.prefix, candidate.suffix) != (value, line.prefix, line.suffix):
            raise ValueError(f"valor inválido para {line.key}: aspas alteram a estrutura da entrada")
        line.value = value
        line.dirty = True

    def to_bytes(self) -> bytes:
        return "".join(line.text() + line.eol for line in self.lines).encode("utf-8-sig")

    def write(self, path: str | Path) -> None:
        atomic_write(Path(path), self.to_bytes())


def compare(src: LocFile, dst: LocFile, target_languages: set[str],
            source_language: str) -> list[str]:
    """Somente o valor no idioma selecionado pode mudar; a estrutura é imutável."""
    problems = []
    if dst.encoding != "utf-8-sig":
        problems.append("arquivo EU4 de saída sem UTF-8 com BOM")
    if len(src.lines) != len(dst.lines):
        return problems + [f"número de linhas difere: {len(src.lines)} vs {len(dst.lines)}"]
    for number, (original, translated) in enumerate(zip(src.lines, dst.lines), 1):
        label = f"linha {number}" + (f" ({original.key})" if original.key else "")
        if original.eol != translated.eol:
            problems.append(f"{label}: fim de linha diferente")
        if original.is_entry != translated.is_entry:
            problems.append(f"{label}: deixou de ser/passou a ser uma entrada")
            continue
        if not original.is_entry:
            if original.text() != translated.text():
                problems.append(f"{label}: cabeçalho/comentário/linha vazia/linha inválida alterado")
            continue
        if (original.key, original.language, original.prefix, original.suffix) != (
                translated.key, translated.language, translated.prefix, translated.suffix):
            problems.append(f"{label}: chave, idioma, versão, espaços ou comentário alterados")
        if original.value == translated.value:
            continue
        if original.language not in target_languages or original.language != source_language:
            problems.append(f"{label}: idioma não selecionado foi alterado")
            continue
        problems.extend(f"{label}: {problem}" for problem in hard_problems(original.value, translated.value))
    return problems
