"""Lê os arquivos de termos (keep.txt e glossary.txt).

keep.txt      um termo por linha (nomes que NÃO devem ser traduzidos).
glossary.txt  uma regra por linha:   Termo = Tradução      (também aceita  Termo -> Tradução  ou  Termo: Tradução)
Linhas vazias e linhas que começam com # são ignoradas; # no fim da linha também (precisa de espaço antes).
Os arquivos são UTF-8 (com ou sem BOM). Ponto e vírgula não é permitido na tradução (quebraria o .csv).
"""
from __future__ import annotations

import re
from pathlib import Path

_SEP = re.compile(r"\s*(?:=|->|=>)\s*|\s*:\s+")


def _lines(path: Path) -> list[tuple[int, str]]:
    out = []
    for n, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        s = re.sub(r"\s+#.*$", "", raw).strip()
        if s and not s.startswith("#"):
            out.append((n, s))
    return out


def _unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1].strip() if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'" else s


def read_keep(path: Path) -> list[str]:
    return [t for _, line in _lines(path) if (t := _unquote(line))]


def read_glossary(path: Path) -> dict[str, str]:
    """Levanta ValueError com o número da linha se alguma regra estiver malformada."""
    gl: dict[str, str] = {}
    for n, line in _lines(path):
        parts = _SEP.split(line, maxsplit=1)
        if len(parts) != 2 or not _unquote(parts[0]) or not _unquote(parts[1]):
            raise ValueError(f"{path.name}, linha {n}: use  Termo = Tradução   (recebi: {line!r})")
        k, v = _unquote(parts[0]), _unquote(parts[1])
        if ";" in v:
            raise ValueError(f"{path.name}, linha {n}: a tradução não pode ter ';'")
        gl[k] = v
    return gl


def collect(base: Path, cfg_paths, default_name: str) -> list[Path]:
    """Arquivos a ler: os listados na config (string ou lista, relativos à pasta da config);
    se nada foi listado, o arquivo padrão (keep.txt / glossary.txt) quando existir."""
    if cfg_paths is None:
        p = base / default_name
        return [p] if p.exists() else []
    if isinstance(cfg_paths, str):
        cfg_paths = [cfg_paths] if cfg_paths.strip() else []      # "" = não usar arquivo
    return [(base / Path(p).expanduser()).resolve() for p in cfg_paths]
