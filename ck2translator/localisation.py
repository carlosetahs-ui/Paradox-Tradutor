"""Operações de localisation comuns aos formatos dos jogos suportados."""
from __future__ import annotations

from pathlib import Path

from . import ck2loc, eu4loc
from .profiles import get_profile

_FORMATS = {"ck2": ck2loc, "eu4": eu4loc}


def read(path: Path, game: str):
    return _FORMATS[get_profile(game).id].LocFile.read(path)


def source_entries(loc, language: str):
    """Produz (linha, texto), somente para entradas traduzíveis do idioma escolhido."""
    for line in loc.entries():
        value = cell(loc, line, language)
        if line.key and value is not None and value.strip():
            yield line, value


def cell(loc, line, language: str) -> str | None:
    if isinstance(loc, ck2loc.LocFile):
        index = loc.columns[language]
        return line.fields[index] if len(line.fields) > index else None
    return line.value if line.language == language else None


def set_translation(loc, line, languages: list[str], value: str) -> None:
    if isinstance(loc, ck2loc.LocFile):
        indexes = [loc.columns[name] for name in languages]
        if any(index >= len(line.fields) or
               (index == len(line.fields) - 1 and line.fields[index].strip().lower() == "x")
               for index in indexes):
            raise ValueError("coluna de destino ausente ou ocupada pelo marcador final x; estrutura preservada")
        for index in indexes:
            loc.set_cell(line, index, value)
    elif line.language in languages:
        loc.set_value(line, value)
    else:
        raise ValueError("idioma de destino diferente do cabeçalho original")


def hard_problems(src: str, dst: str, game: str) -> list[str]:
    return _FORMATS[get_profile(game).id].hard_problems(src, dst)


def compare(src, dst, source_language: str, target_languages: list[str]) -> list[str]:
    if isinstance(src, ck2loc.LocFile):
        return ck2loc.compare(src, dst, {src.columns[name] for name in target_languages},
                              src.columns[source_language])
    return eu4loc.compare(src, dst, set(target_languages), source_language)
