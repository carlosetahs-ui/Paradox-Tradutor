"""Avisos de qualidade que não alteram o texto nem substituem revisão humana."""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping

from .ck2loc import TOKEN_RE

_WORDS = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
_ENGLISH = frozenset("the and your you will with this that have has from when must "
                     "cannot while until their which would should of to for in".split())
_VERBS = frozenset("gains gain loses lose receives receive grants grant increases increase "
                   "reduces reduce becomes become unlocks unlock allows allow returns return".split())


def _repetitions(text: str) -> list[tuple[str, int, int]]:
    """Frases de até quatro palavras repetidas, separadas apenas por espaços."""
    words = list(_WORDS.finditer(text))
    folded = [word.group().casefold() for word in words]
    found = []
    index = 0
    while index < len(words):
        for width in range(1, min(4, (len(words) - index) // 2) + 1):
            unit = folded[index:index + width]
            end = index + width
            check_from = index + 1
            while folded[end:end + width] == unit:
                candidate = end + width
                if any(not text[words[p - 1].end():words[p].start()].isspace()
                       for p in range(check_from, candidate)):
                    break
                end = candidate
                check_from = candidate
            copies = (end - index) // width
            if copies > 1:
                found.append((text[words[index].start():words[index + width - 1].end()], width, copies))
                index = end
                break
        else:
            index += 1
    return found


def _unbalanced_angles(text: str) -> bool:
    if "«" not in text and "»" not in text:
        return False
    depth = 0
    for char in text:
        if char == "«":
            depth += 1
        elif char == "»":
            depth -= 1
            if depth < 0:
                return True
    return depth != 0


def quality_problems(src: str, dst: str) -> list[str]:
    """Sinaliza repetições novas e aspas angulares desparelhadas.

    Repetições presentes na origem permitem a mesma quantidade de ênfase na
    tradução, mesmo quando as palavras mudam de idioma. Nada é apagado.
    """
    if src == dst:
        return []
    problems = []
    allowed = Counter((width, copies) for _, width, copies in _repetitions(src))
    for phrase, width, copies in _repetitions(dst):
        if allowed[width, copies]:
            allowed[width, copies] -= 1
        else:
            problems.append(f"repetição consecutiva nova: '{phrase}' ({copies} vezes)")
    if _unbalanced_angles(dst) and not _unbalanced_angles(src):
        problems.append("aspas angulares « » desparelhadas na tradução")
    return problems


def unchanged_prose(src: str, dst: str, *, keep: Iterable[str] = (),
                    glossary: Mapping[str, str] | Iterable[str] = (),
                    token_re: re.Pattern[str] = TOKEN_RE) -> bool:
    """Heurística conservadora de prosa inglesa devolvida sem tradução.

    Ignora códigos, nomes preservados e termos do glossário. Não classifica
    nomes isolados nem tenta determinar o idioma de traduções parciais.
    """
    if src.strip() != dst.strip():
        return False
    visible = token_re.sub(" ", src)
    terms = set(keep) | set(glossary)
    if isinstance(glossary, Mapping):
        terms.update(glossary.values())
    if terms:
        pattern = r"(?<!\w)(?:" + "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True)) + r")(?!\w)"
        visible = re.sub(pattern, " ", visible, flags=re.I)
    words = _WORDS.findall(visible)
    folded = [word.casefold() for word in words]
    if len(words) < 4 or sum(word[:1].islower() for word in words) < 3:
        return False
    return sum(word in _ENGLISH for word in folded) >= 2 or any(word in _VERBS for word in folded)
