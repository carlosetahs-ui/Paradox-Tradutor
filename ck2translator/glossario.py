"""Sugere [keep] e [glossary] a partir do cache de tradução existente.

Uso:   python glossario_do_cache.py                      (lê data/cache/cache.sqlite)
       python glossario_do_cache.py outro/cache.sqlite --min 4
Um cache.sqlite existente na pasta atual tem prioridade, para compatibilidade.

Mantém o cache intacto: grava saida/keep_sugerido.txt e saida/glossary_sugerido.txt.
Revise as sugestões e salve os termos aprovados em data/terms/keep.txt e glossary.txt.

  [keep]      nomes próprios que o tradutor deixou iguais na maioria das vezes (ou que
              deveriam ficar iguais): casas, lugares, pessoas.
  [glossary]  termos que viraram sempre a mesma palavra em português (ou que mudam de
              uma linha para outra, e por isso vale fixar). O sistema escolhe a tradução
              mais frequente: confira antes de usar.
Os dois arquivos valem para todos os backends: ollama/openai recebem no prompt; translators/argos/google
recebem como marcadores (o glossário entra com a tradução já pronta).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

WORD = re.compile(r"[A-Za-zÀ-ÿ'][A-Za-zÀ-ÿ'\-]*")
# Sequência de palavras Capitalizadas (1 a 4), aceita "of/the/de" no meio: "Night's Watch", "Casterly Rock", "Lord of Light"
NAME = re.compile(r"\b[A-Z][\w'\-]+(?:\s+(?:of|the|de|da|do|von|van)?\s*[A-Z][\w'\-]+){0,3}")
MASK = re.compile(r"\{\d+\}")
STOP = {"The", "A", "An", "And", "Of", "In", "On", "At", "To", "For", "He", "She", "It", "They", "We", "You", "I",
        "His", "Her", "Its", "Their", "This", "That", "These", "Those", "If", "But", "Or", "As", "With", "From",
        "By", "Is", "Are", "Was", "Were", "Be", "Has", "Have", "Had", "Not", "No", "Yes", "All", "Any", "Our", "My",
        "Your", "When", "While", "Who", "What", "Where", "Why", "How", "After", "Before", "Once", "Now", "Then"}
PT_STOP = {"de", "da", "do", "das", "dos", "e", "o", "a", "os", "as", "um", "uma", "em", "no", "na", "nos", "nas",
           "que", "se", "por", "para", "com", "ao", "à", "é", "foi", "são", "ser", "como", "mais", "seu", "sua",
           "seus", "suas", "ele", "ela", "não", "ou", "mas", "ter", "tem", "pelo", "pela", "sobre", "entre"}


def clean(s: str) -> str:
    return MASK.sub(" ", s)


def tokens(s: str) -> list[str]:
    return [w.lower() for w in WORD.findall(clean(s))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    default_cache = "cache.sqlite" if Path("cache.sqlite").is_file() else "data/cache/cache.sqlite"
    ap.add_argument("cache", nargs="?", default=default_cache)
    ap.add_argument("--min", type=int, default=3, help="mínimo de ocorrências (padrão 3)")
    ap.add_argument("--out", default="saida", help="pasta onde gravar keep_sugerido.txt e glossary_sugerido.txt")
    a = ap.parse_args(argv)

    if not Path(a.cache).is_file():
        print(f"Não achei {a.cache}. Rode na pasta do projeto ou passe o caminho.")
        return 1
    try:
        db = sqlite3.connect(Path(a.cache).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT src, dst FROM cache WHERE model != 'echo' AND src != dst").fetchall()
        finally:
            db.close()
    except (OSError, sqlite3.Error) as error:
        print(f"ERRO ao ler o cache {a.cache}: {error}", file=sys.stderr)
        return 2
    if not rows:
        print("Cache vazio (ou só com o backend de teste).")
        return 1

    # Remove duplicatas (o mesmo texto pode estar uma vez por backend).
    pares = list(dict.fromkeys((s, d) for s, d in rows))
    print(f"{len(pares)} traduções lidas do cache.")

    # ---- candidatos: nomes Capitalizados que não estão só no começo de frase
    ocorr: Counter[str] = Counter()
    mantido: Counter[str] = Counter()
    entradas: dict[str, list[int]] = defaultdict(list)
    for i, (s, d) in enumerate(pares):
        s_limpo, d_limpo = clean(s), clean(d)
        d_low = d_limpo.lower()
        achados = set()
        for m in NAME.finditer(s_limpo):
            t = m.group(0).strip()
            partes = t.split()
            while partes and partes[0] in STOP:
                partes = partes[1:]
            if not partes:
                continue
            t = " ".join(partes)
            if len(t) < 3 or t in STOP:
                continue
            # palavra única no começo da frase é quase sempre só maiúscula de início
            antes = s_limpo[:m.start()].rstrip()
            if len(partes) == 1 and (not antes or antes[-1] in '.!?:"“\u2014-*'):
                continue
            if "Get" in t or "_" in t:           # nome de função/variável do jogo
                continue
            achados.add(t)
        for t in achados:
            ocorr[t] += 1
            entradas[t].append(i)
            if t.lower() in d_low:
                mantido[t] += 1

    keep, glos = [], []
    base_df: Counter[str] = Counter()
    for _, d in pares:
        base_df.update(set(tokens(d)))
    n = len(pares)

    for t, c in ocorr.most_common():
        if c < a.min:
            continue
        taxa = mantido[t] / c
        if taxa >= 0.8:
            keep.append((t, c, taxa))
            continue
        # Nome composto em que só uma parte ficou igual (ex.: House Stark -> Casa Stark): mantém essa parte
        partes_t = [w for w in t.split() if w[0].isupper() and len(w) >= 3 and w not in STOP]
        if len(partes_t) > 1:
            sobra = [w for w in partes_t if sum(w.lower() in clean(pares[i][1]).lower() for i in entradas[t]) / c >= 0.8]
            if sobra:
                for w in sobra:
                    keep.append((w, c, 1.0))
                continue
        # Foi traduzido: descobre a palavra em português mais associada ao termo
        tl = set(tokens(t))
        co: Counter[str] = Counter()
        for i in entradas[t]:
            co.update(set(tokens(pares[i][1])) - PT_STOP - tl)
        melhor = None
        for w, k in co.most_common(40):
            if len(w) < 3:
                continue
            frac = k / c                      # presença nas linhas que têm o termo
            lift = frac / max(base_df[w] / n, 1e-9)
            if frac >= 0.6 and lift >= 2:
                melhor = (w, frac)
                break
        if melhor and len(t.split()) <= 2:
            glos.append((t, melhor[0], c, melhor[1]))

    # palavra comum do inglês (aparece também em minúscula no texto) não é nome próprio
    minusc: Counter[str] = Counter()
    for sx, _ in pares:
        minusc.update(w for w in WORD.findall(clean(sx)) if w[0].islower())
    keep = [(t, c, r) for t, c, r in keep if " " in t or minusc[t.lower()] < 3]
    glos = [g for g in glos if " " not in g[0]]      # multi-palavra: a heurística erra; só termos de 1 palavra
    keep = sorted({t: (t, c, r) for t, c, r in keep}.values(), key=lambda x: -x[1])

    # ---- saída
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "keep_sugerido.txt").write_text(
        "# Nomes que não devem ser traduzidos (um por linha). Gerado do cache: REVISE e salve como keep.txt\n"
        + "\n".join(t for t, _, _ in keep[:200]) + "\n", encoding="utf-8")
    (out / "glossary_sugerido.txt").write_text(
        "# Termo = Tradução   (gerado do cache; as linhas começam COMENTADAS: tire o # só das que estiverem certas)\n"
        + "\n".join(f"# {t} = {pt}   # {c}x, {frac:.0%}" for t, pt, c, frac in glos[:120]) + "\n", encoding="utf-8")

    print(f"\n[keep] {len(keep)} candidatos (nome aparece igual na tradução em ≥80% dos casos):")
    for t, c, taxa in keep[:40]:
        print(f"  {c:>5}x  {taxa:>4.0%}  {t}")
    print(f"\n[glossary] {len(glos)} candidatos (palavra em português mais associada; CONFIRA):")
    for t, pt, c, frac in glos[:40]:
        print(f"  {c:>5}x  {t}  ->  {pt}")
    print(f"\nArquivos gravados em {out}: keep_sugerido.txt e glossary_sugerido.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
