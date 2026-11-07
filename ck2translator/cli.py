#!/usr/bin/env python3
"""ck2tr.py - tradutor de localisation do Crusader Kings II com modelo local.

  python ck2tr.py scan      --config config.toml     # só analisa 
  python ck2tr.py translate --config config.toml     # traduz os mods listados
  python ck2tr.py verify    --config config.toml     # confere original x traduzido

"""
from __future__ import annotations

import argparse
import collections
import csv
import fnmatch
import re
import shutil
import sqlite3
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

from .ck2loc import TOKEN_RE, LocFile, compare, hard_problems
from . import revisao
from .config import BACKENDS, ConfigError, Mod, load_config, resolve_mods
from .files import atomic_path, atomic_write
from . import __version__
from .translator import (ArgosBackend, Backend, BackendError, Cache, EchoBackend, GoogleBackend, OllamaBackend,
                        OpenAICompatBackend, RateLimiter, Translator, TranslatorsBackend, fit_cp1252,
                        install_argos_package, mask, normalize_punct)


# --------------------------------------------------------------------------- configuração
def loc_files(mod: Mod, cfg: dict) -> list[Path]:
    skip = [s.lower() for s in cfg["translate"].get("skip_files", [])]
    return sorted(p for p in mod.loc_dir.iterdir()
                  if p.is_file() and p.suffix.lower() == ".csv"
                  and not any(fnmatch.fnmatch(p.name.lower(), pat) for pat in skip))


def build_backend(cfg: dict, override: str | None, *, offline: bool = False):
    m = cfg["model"]
    kind = override or m.get("backend", "ollama")
    if kind not in BACKENDS:
        raise ConfigError(f"backend desconhecido: {kind}. Use: {', '.join(BACKENDS)}")
    temp, timeout = float(m.get("temperature", 0.2)), float(m.get("timeout", 180))
    limiter = None
    if kind == "translators":
        limiter = RateLimiter(int(m.get("max_per_minute", 20)), int(m.get("max_per_hour", 600)),
                              (float(m.get("pause_min", 1.5)), float(m.get("pause_max", 4.0))),
                              int(m.get("long_pause_every", 80)),
                              (float(m.get("long_pause_min", 40)), float(m.get("long_pause_max", 90))))
    elif kind == "google":
        pause = float(m.get("long_pause", 30))
        limiter = RateLimiter(0, 0, (float(m.get("pause_min", 1)), float(m.get("pause_max", 2))),
                              int(m.get("long_pause_every", 100)), (pause, pause))
    if offline:
        backend = Backend()
        backend.model_id = f"{kind}:{m.get('name', '')}" if kind in ("ollama", "openai") else kind
        backend.uses_prompt = kind in ("ollama", "openai", "echo")
        backend.limiter = limiter
        return backend
    if kind == "echo":
        return EchoBackend()
    if kind == "translators":
        return TranslatorsBackend(m.get("providers", ["google", "bing", "alibaba", "yandex"]),
                                  m.get("source_lang", "en"), m.get("target_lang", "pt"), limiter,
                                  int(m.get("max_retries", 4)), float(m.get("backoff_base", 15)),
                                  float(m.get("cooldown", 600)), int(m.get("max_chars", 3000)))
    if kind == "google":
        return GoogleBackend(m.get("source_lang", "en"), m.get("target_lang", "pt"),
                             (float(m.get("pause_min", 1.0)), float(m.get("pause_max", 2.0))),
                             int(m.get("long_pause_every", 100)), float(m.get("long_pause", 30)))
    if kind == "argos":
        return ArgosBackend(m.get("source_lang", "en"), m.get("target_lang", "pt"))
    name = m.get("name")
    if not name:
        raise ConfigError("Defina [model] name na config (ex.: name = \"qwen2.5:7b-instruct\").")
    if kind == "ollama":
        return OllamaBackend(name, m.get("host", "http://localhost:11434"), temp, timeout)
    if kind == "openai":
        return OpenAICompatBackend(name, m.get("host", "http://localhost:1234/v1"), temp, timeout,
                                   m.get("api_key", ""))
    raise ConfigError(f"backend desconhecido: {kind}")


# --------------------------------------------------------------------------- scan
def cmd_scan(cfg: dict, mods: list[Mod]) -> int:
    tr = cfg["translate"]
    src_name = tr["source_column"]
    for mod in mods:
        print(f"\n== {mod.name}  ({mod.path})")
        uniq: set[str] = set()
        total_chars = total_entries = 0
        for p in loc_files(mod, cfg):
            loc = LocFile.read(p)
            sc = loc.columns[src_name]
            n_entries = n_src = 0
            odd = 0
            for ln in loc.entries():
                n_entries += 1
                if len(ln.fields) != 15:
                    odd += 1
                if len(ln.fields) > sc and ln.key and ln.fields[sc].strip():
                    n_src += 1
                    uniq.add(ln.fields[sc])
                    total_chars += len(ln.fields[sc])
            total_entries += n_src
            flag = f"  [{odd} linhas com nº de colunas != 15]" if odd else ""
            print(f"  {p.name:<45} {loc.encoding:<9} {n_entries:>6} entradas, "
                  f"{n_src:>6} com texto em '{src_name}'{flag}")
        print(f"  -> {total_entries} textos ({len(uniq)} únicos), ~{total_chars:,} caracteres")
    seen_in: dict[str, list[str]] = {}
    for mod in mods:
        for p in loc_files(mod, cfg):
            seen_in.setdefault(p.name.lower(), []).append(mod.name)
    clash = {n: m for n, m in seen_in.items() if len(set(m)) > 1}
    if clash:
        print("\nATENÇÃO: arquivos com o MESMO NOME em mods diferentes (no jogo, o que carrega por último vence).")
        print("Se o mod de tradução de um deles carregar depois, pode sobrescrever o do outro. Confira a ordem de carga:")
        for n, m in sorted(clash.items()):
            print(f"  {n}: {', '.join(dict.fromkeys(m))}")
    return 0


# --------------------------------------------------------------------------- translate
def _fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}min" if sec >= 3600 else f"{sec // 60}min{sec % 60:02d}s"


def _progress(done: int, total: int, counts: dict[str, int], t_model: float | None, calls: int) -> None:
    eta = ""
    if t_model is not None and calls >= 3:
        per = (time.time() - t_model) / calls            # segundos por texto que realmente foi ao modelo
        eta = f" | {per:.1f}s/texto, faltam até ~{_fmt_time(per * (total - done))}"
    sys.stdout.write(f"\r  traduzindo {done}/{total}  ok={counts['ok']} cache={counts['cache']} "
                     f"pulados={counts['skip']} falhas={counts['falha']}{eta}      ")
    sys.stdout.flush()


def translate_mod(mod: Mod, cfg: dict, translator: Translator, limit: int | None) -> int:
    tr = cfg["translate"]
    out_root: Path = tr["output_dir"]
    src_name, write_names = tr["source_column"], tr["write_columns"]

    if tr["mode"] == "inplace":
        backup = out_root / "backup" / mod.slug
        if backup.exists():
            sys.exit(f"Já existe backup em {backup}. Este mod provavelmente já foi traduzido "
                     "in-place; traduzir de novo traduziria o português. Restaure o backup ou use overlay.")

    locs = [LocFile.read(p) for p in loc_files(mod, cfg)]
    texts: dict[str, None] = {}
    for loc in locs:
        sc = loc.columns[src_name]
        for ln in loc.entries():
            if len(ln.fields) > sc and ln.key and ln.fields[sc].strip():
                texts.setdefault(ln.fields[sc], None)
    todo = list(texts)[:limit] if limit is not None else list(texts)
    print(f"\n== {mod.name}: {len(locs)} arquivos, {len(todo)} textos únicos para traduzir")

    results, counts = {}, {"ok": 0, "cache": 0, "skip": 0, "falha": 0}
    lock = Lock()
    t_model, last_print = None, 0.0
    with ThreadPoolExecutor(max_workers=int(tr["workers"])) as ex:
        futures = {ex.submit(translator.translate, t): t for t in todo}
        try:
            for i, fut in enumerate(as_completed(futures), 1):
                res = fut.result()                      # BackendError sobe daqui
                with lock:
                    results[futures[fut]] = res
                    counts[res.status] += 1
                if t_model is None and res.status in ("ok", "falha"):
                    t_model = time.time()          # o relógio só começa no 1º texto que foi ao modelo
                now = time.time()
                if i == len(todo) or now - last_print >= 2:
                    _progress(i, len(todo), counts, t_model, counts["ok"] + counts["falha"])
                    last_print = now
        except BaseException:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
    print()

    # aplica os resultados e grava
    report: list[list[str]] = []
    written = reverted = 0
    for loc in locs:
        sc = loc.columns[src_name]
        write_idx = [loc.columns[n] for n in write_names]
        changed = 0
        for ln in loc.entries():
            if len(ln.fields) <= sc:
                continue
            res = results.get(ln.fields[sc])
            if res is None or res.status == "skip":
                continue
            if res.status == "falha":
                report.append([loc.path.name, ln.key, "falha", ln.fields[sc], res.note])
                continue
            if any(ci >= len(ln.fields) or
                   (ci == len(ln.fields) - 1 and ln.fields[ci].strip().lower() == "x") for ci in write_idx):
                report.append([loc.path.name, ln.key, "revertida", ln.fields[sc],
                               "coluna de destino ausente ou ocupada pelo marcador final x; estrutura preservada"])
                reverted += 1
                continue
            new = fit_cp1252(res.text) if not loc.encoding.startswith("utf-8") else res.text
            if tr["safety"] != "normal":                   # trava de segurança: na dúvida, fica o original
                new = normalize_punct(ln.fields[sc], new)
                bad = hard_problems(ln.fields[sc], new)
                if bad:
                    report.append([loc.path.name, ln.key, "revertida", ln.fields[sc], "; ".join(bad)])
                    reverted += 1
                    continue
            for ci in write_idx:
                loc.set_cell(ln, ci, new)
            changed += 1
        overlay = out_root / f"{mod.slug}_ptbr" / "localisation" / loc.path.name
        if not changed and not (tr["mode"] == "overlay" and overlay.exists()):
            continue
        if tr["mode"] == "overlay":
            dest = overlay
        else:
            backup_file = out_root / "backup" / mod.slug / "localisation" / loc.path.name
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(loc.path, backup_file)
            dest = loc.path
        loc.write(dest)
        written += 1
        print(f"  gravado: {dest}  ({changed} linhas)")

    if tr["mode"] == "overlay" and written:
        write_mod_descriptor(out_root, mod)
    rep = out_root / f"{mod.slug}_relatorio.csv"
    if report or rep.exists():
        out_root.mkdir(parents=True, exist_ok=True)
        with atomic_path(rep) as temporary:
            with temporary.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["arquivo", "chave", "status", "original", "motivo"])
                w.writerows(report)
        if report:
            print(f"  {len(report)} linhas ficaram em inglês (veja {rep})")
    if reverted:
        print(f"  {reverted} linha(s) voltaram ao inglês pela trava de segurança (veja o relatório)")
    print(f"  resumo: {counts}")
    return len(report)


def write_mod_descriptor(out_root: Path, mod: Mod) -> None:
    safe = mod.name.replace('"', "'")
    text = (f'name = "{safe} [PT-BR]"\n'
            f'path = "mod/{mod.slug}_ptbr"\n'
            f'dependencies = {{ "{safe}" }}\n')
    out_root.mkdir(parents=True, exist_ok=True)
    atomic_write(out_root / f"{mod.slug}_ptbr.mod", text.encode("cp1252", errors="replace"))


NO_PING = ("translators", "google")      # backends online: o pré-teste gastaria um pedido à toa


def make_translator(cfg: dict, backend_override: str | None, *, offline: bool = False):
    backend = build_backend(cfg, backend_override, offline=offline)
    cache = Cache(str(cfg["translate"]["cache"]), read_only=offline)
    name = cfg["model"].get("name", "")
    rc = cfg["translate"]["reuse_cache_from"]
    reuse = "auto" if rc == "auto" else [f"{r}:{name}" if r in ("ollama", "openai") else r for r in rc]
    post = revisao.build(bool(cfg["translate"]["ptbr_fix"]), cfg.get("replacements"))
    translator = Translator(backend, cache, cfg["glossary_terms"], cfg["keep_terms"],
                            cfg["translate"].get("context", ""), reuse_from=reuse,
                            paranoid=cfg["translate"]["safety"] == "paranoid", post=post)
    return backend, cache, translator


def cmd_translate(cfg: dict, mods: list[Mod], backend_override: str | None, limit: int | None) -> int:
    backend, cache, translator = make_translator(cfg, backend_override)
    try:
        print(f"Modelo: {backend.model_id}  | modo: {cfg['translate']['mode']}  | segurança: {cfg['translate']['safety']}")
        print(f"Termos: {len(cfg['keep_terms'])} para manter, {len(cfg['glossary_terms'])} no glossário")
        if cfg["replacements"] or cfg["translate"]["ptbr_fix"]:
            print(f"Pós-edição: ptbr_fix={'sim' if cfg['translate']['ptbr_fix'] else 'não'}, "
                  f"{len(cfg['replacements'])} substituições suas")
        if backend.model_id.split(":")[0] not in NO_PING:
            backend.chat("Responda apenas: OK", "<texto>\nping\n</texto>")      # pré-teste
        ids = translator.reuse_ids()
        if ids:
            print(f"Reaproveitando o que já foi traduzido por: {', '.join(ids)}")
        failures = sum(translate_mod(m, cfg, translator, limit) for m in mods)
        if ids:
            print(f"Textos reaproveitados de outros backends: {translator.reused}")
        if getattr(backend, "stats", None):
            print(f"Pedidos por serviço: {backend.stats}")
    except BackendError as e:
        print(f"\nERRO do modelo: {e}\nO progresso foi salvo no cache; rode de novo para continuar.")
        return 2
    except KeyboardInterrupt:
        print("\nInterrompido. O progresso foi salvo no cache; rode de novo para continuar.")
        return 130
    finally:
        cache.close()
    print("\nPróximo passo: rode 'verify' e depois copie a(s) pasta(s) *_ptbr e o(s) .mod "
          "de saida/ para a pasta mod do CK2.")
    return 1 if failures else 0


# --------------------------------------------------------------------------- status
def _est_seconds_per_text(backend) -> float | None:
    """Estimativa de segundos por texto para backends com limitador (translators/google)."""
    lim = getattr(backend, "limiter", None)
    if lim is None:
        return None
    per = sum(lim.pause) / 2 + (sum(lim.long_pause) / 2 / lim.long_every if lim.long_every else 0)
    return max(per, 60 / lim.per_minute if lim.per_minute else 0)


def cmd_status(cfg: dict, mods: list[Mod], backend_override: str | None) -> int:
    """Quanto já está no cache e quanto falta, SEM chamar nenhum serviço de tradução."""
    backend, cache, translator = make_translator(cfg, backend_override, offline=True)
    src_name = cfg["translate"]["source_column"]
    try:
        total_pend = 0
        for mod in mods:
            texts: dict[str, None] = {}
            for p in loc_files(mod, cfg):
                loc = LocFile.read(p)
                sc = loc.columns[src_name]
                for ln in loc.entries():
                    if len(ln.fields) > sc and ln.key and ln.fields[sc].strip():
                        texts.setdefault(ln.fields[sc], None)
            c = collections.Counter(translator.translate(t, dry=True).status for t in texts)
            pend = c["pendente"]
            total_pend += pend
            done = c["cache"] + c["ok"]
            pct = 100 * (done + c["skip"]) / max(1, len(texts))
            print(f"{mod.name}: {len(texts)} textos únicos | prontos {done} | pulados {c['skip']} | "
                  f"faltam {pend}  ({pct:.0f}% resolvido)")
        per = _est_seconds_per_text(backend)
        if total_pend and per:
            print(f"\nEstimativa para o que falta: ~{_fmt_time(per * total_pend)} (com as pausas anti-bloqueio).")
        elif not total_pend:
            print("\nTudo já está no cache: 'translate' só regrava os arquivos.")
    finally:
        cache.close()
    return 0


# --------------------------------------------------------------------------- package
LEIA_ME = """{name} - Tradução PT-BR (automática)

COMO INSTALAR
1. Copie a pasta "{slug}_ptbr" e o arquivo "{slug}_ptbr.mod" para a pasta de mods do Crusader Kings II:
   Documentos\\Paradox Interactive\\Crusader Kings II\\mod
2. No launcher do jogo, escolha um destes idiomas: {languages}, e ative
   "{name}" e depois "{name} [PT-BR]". A tradução precisa ficar DEPOIS do mod original.

SOBRE A TRADUÇÃO
Feita automaticamente (tradução de máquina) e não revisada por completo: pode haver erros de contexto,
gênero e concordância. Os códigos do jogo ([Root.GetName], $VAR$, §Y...) foram preservados. Se uma linha
ficou errada, é só ela: o jogo não depende da tradução para funcionar. Para desativar, desmarque o mod.
"""


def cmd_package(cfg: dict, mods: list[Mod]) -> int:
    out_root: Path = cfg["translate"]["output_dir"]
    if cfg["translate"]["mode"] != "overlay":
        print("O comando package exige translate.mode = 'overlay'.")
        return 1
    n = 0
    failures = 0
    for mod in mods:
        folder = out_root / f"{mod.slug}_ptbr"
        descr = out_root / f"{mod.slug}_ptbr.mod"
        if not (folder / "localisation").is_dir() or not descr.exists():
            print(f"{mod.name}: nada para empacotar (rode 'translate' em modo overlay antes).")
            failures += 1
            continue
        if cmd_verify(cfg, [mod]):
            print(f"{mod.name}: pacote não gerado; corrija os problemas encontrados pelo verify.")
            failures += 1
            continue
        zpath = out_root / f"{mod.slug}_ptbr.zip"
        languages = {"english": "INGLÊS", "french": "FRANCÊS", "german": "ALEMÃO", "spanish": "ESPANHOL"}
        readme = LEIA_ME.format(name=mod.name, slug=mod.slug,
                               languages=", ".join(languages[c] for c in cfg["translate"]["write_columns"]))
        with atomic_path(zpath) as temporary:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(descr, descr.name)
                for f in sorted(folder.rglob("*")):
                    if f.is_file():
                        z.write(f, f.relative_to(out_root).as_posix())
                z.writestr("LEIA-ME.txt", readme.replace("\n", "\r\n"))
        print(f"{mod.name}: {zpath}")
        n += 1
    if n:
        print("\nPacotes gerados após verificação dos arquivos de tradução.")
    return 1 if failures or not n else 0


# --------------------------------------------------------------------------- terms
_TOK_RE = re.compile(r"\{\d+\}|[A-Za-z\u00c0-\u00ff][A-Za-z\u00c0-\u00ff'\u2019\-]*|[.!?:\"\u201c\n]")
_CONNECT = {"of", "de", "da", "do", "del", "von", "van", "the", "la", "le"}


def suggest_terms(texts: list[str], min_count: int = 5) -> list[tuple[str, int]]:
    """Acha nomes próprios: palavras que aparecem com inicial maiúscula no MEIO das frases e quase nunca em minúscula."""
    cap_mid: collections.Counter = collections.Counter()
    lower: collections.Counter = collections.Counter()
    toks_all = []
    for t in texts:
        toks = _TOK_RE.findall(mask(t)[0].replace("\\n", "\n"))
        toks_all.append(toks)
        start = True
        for tok in toks:
            if tok in ".!?:\"\u201c\n":
                start = True
            elif tok.startswith("{"):
                start = False
            else:
                if tok[0].isupper():
                    if not start:
                        cap_mid[tok] += 1
                else:
                    lower[tok] += 1
                start = False
    name_like = {w for w, c in cap_mid.items()
                 if c >= min_count and len(w) > 1 and lower[w] <= max(1, c // 10)}
    phrases: collections.Counter = collections.Counter()
    for toks in toks_all:
        chain: list[str] = []
        for tok in toks + ["."]:
            if tok in name_like:
                chain.append(tok)
            elif chain and tok.lower() in _CONNECT:
                chain.append(tok)
            else:
                while chain and chain[-1].lower() in _CONNECT:
                    chain.pop()
                if chain:
                    phrases[" ".join(chain)] += 1
                chain = []
    return phrases.most_common()


def cmd_terms(cfg: dict, mods: list[Mod], min_count: int) -> int:
    src_name = cfg["translate"]["source_column"]
    seen: set[str] = set()
    for mod in mods:
        for p in loc_files(mod, cfg):
            loc = LocFile.read(p)
            sc = loc.columns[src_name]
            for ln in loc.entries():
                if len(ln.fields) > sc and ln.key and ln.fields[sc].strip():
                    seen.add(ln.fields[sc])
    found = suggest_terms(sorted(seen), min_count)
    out = cfg["translate"]["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "termos_sugeridos.txt"
    atomic_write(dest, "\n".join(f"{n:>6}  {t}" for t, n in found).encode("utf-8"))
    print(f"{len(found)} candidatos (veja a lista completa em {dest}). Os mais frequentes:\n")
    for t, n in found[:60]:
        print(f"  {n:>5}  {t}")
    top = ", ".join(f'"{t}"' for t, _ in found[:40])
    print(f"\nModelo para o config.toml (REVISE antes de usar; apague o que deve ser traduzido):\n\n[keep]\nterms = [{top}]")
    return 0


# --------------------------------------------------------------------------- diff
_STRUCT = "[]$\u00a7\u00a3{}\\"


def struct_warnings(src: str, dst: str) -> list[str]:
    """Diferenças estruturais entre o texto original e o traduzido que podem quebrar o jogo."""
    w: list[str] = []
    for ch in _STRUCT:
        if src.count(ch) != dst.count(ch):
            w.append(f"quantidade de '{ch}' mudou: {src.count(ch)} -> {dst.count(ch)}")
    a, b = TOKEN_RE.findall(src), TOKEN_RE.findall(dst)
    if sorted(a) != sorted(b):
        w.append(f"códigos do jogo diferentes: {a} -> {b}")
    elif a != b:
        w.append("os códigos do jogo mudaram de ORDEM (normal em tradução, mas confira)")
    if len(src) > 40 and len(dst) > 1.8 * len(src):
        w.append(f"texto muito mais longo que o original ({len(src)} -> {len(dst)} caracteres)")
    if src[:len(src) - len(src.lstrip())] != dst[:len(dst) - len(dst.lstrip())] or \
            src[len(src.rstrip()):] != dst[len(dst.rstrip()):]:
        w.append("espaços no início/fim mudaram")
    if any(ord(c) < 32 for c in dst):
        w.append("há caracteres de controle no texto traduzido")
    return w


def _translation_pairs(cfg: dict, mod: Mod) -> list[tuple[Path, Path]]:
    tr = cfg["translate"]
    if tr["mode"] == "overlay":
        directory = tr["output_dir"] / f"{mod.slug}_ptbr" / "localisation"
    else:
        directory = tr["output_dir"] / "backup" / mod.slug / "localisation"
    if not directory.is_dir():
        return []
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
    return ([(mod.loc_dir / p.name, p) for p in files] if tr["mode"] == "overlay"
            else [(p, mod.loc_dir / p.name) for p in files])


def cmd_diff(cfg: dict, mods: list[Mod], find: str | None) -> int:
    """Mostra original x traduzido. Sem --find: só as linhas com algum aviso. Com --find: todas as que contêm o texto."""
    tr = cfg["translate"]
    src_col, shown, total_warn = tr["source_column"], 0, 0
    for mod in mods:
        for src_path, dst_path in _translation_pairs(cfg, mod):
            if not src_path.exists() or not dst_path.exists():
                continue
            src, dst = LocFile.read(src_path), LocFile.read(dst_path)
            head = f"\n=== {mod.name} / {dst_path.name}  (encoding original: {src.encoding}, traduzido: {dst.encoding})"
            printed_head = False
            if len(src.lines) != len(dst.lines):
                print(head + f"\n  ! número de linhas difere: {len(src.lines)} -> {len(dst.lines)}")
                continue
            sc = src.columns[src_col]
            for n, (a, b) in enumerate(zip(src.lines, dst.lines), start=1):
                if not (a.is_entry and b.is_entry) or len(a.fields) <= sc:
                    continue
                for column in tr["write_columns"]:
                    ci = dst.columns[column]
                    if ci >= len(b.fields) or (ci < len(a.fields) and a.fields[ci] == b.fields[ci]):
                        continue
                    ta, tb = a.fields[sc], b.fields[ci]
                    warns = struct_warnings(ta, tb)
                    hit = find and any(find.lower() in x.lower() for x in (a.key, ta, tb))
                    if not (hit if find else warns):
                        continue
                    if not printed_head:
                        print(head)
                        printed_head = True
                    shown += 1
                    total_warn += bool(warns)
                    print(f"  linha {n}  chave: {a.key}  coluna: {column}\n    ORIGINAL: {ta}\n    PT: {tb}")
                    for warning in warns:
                        print(f"    ! {warning}")
    print(f"\n{shown} linha(s) mostrada(s), {total_warn} com aviso.")
    return 0


# --------------------------------------------------------------------------- verify
def cmd_verify(cfg: dict, mods: list[Mod]) -> int:
    tr = cfg["translate"]
    problems_total = checked = 0
    for mod in mods:
        pairs = _translation_pairs(cfg, mod)
        if not pairs:
            print(f"{mod.name}: nada para verificar (já rodou 'translate'?)")
            problems_total += 1
            continue
        for src_path, dst_path in pairs:
            if not src_path.exists() or not dst_path.exists():
                print(f"  FALTA: {src_path if not src_path.exists() else dst_path}")
                problems_total += 1
                continue
            src, dst = LocFile.read(src_path), LocFile.read(dst_path)
            target = {src.columns[n] for n in tr["write_columns"]}
            probs = compare(src, dst, target, src.columns[tr["source_column"]])
            checked += 1
            problems_total += len(probs)
            print(f"  {'OK ' if not probs else 'ERRO'} {dst_path.name}")
            for p in probs[:10]:
                print(f"       - {p}")
            if len(probs) > 10:
                print(f"       ... e mais {len(probs) - 10}")
    print(f"\n{checked} arquivo(s) verificado(s), {problems_total} problema(s).")
    return 1 if problems_total else 0


# --------------------------------------------------------------------------- main
def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use um número inteiro maior que zero") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("use um número inteiro maior que zero")
    return number


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tradutor de mods do Crusader Kings II para português do Brasil.")
    ap.add_argument("--version", action="version", version=f"CK2 Tradutor {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("argos-setup", help="baixa o pacote de idiomas en->pt do Argos (uma vez só, precisa de internet)")
    for name in ("scan", "translate", "verify", "terms", "diff", "status", "package"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default="config.toml")
        sp.add_argument("--mod", help="traduz só os mods cujo nome contém este trecho")
        if name == "diff":
            sp.add_argument("--find", help="mostra todas as linhas (original x traduzido) que contêm este texto ou chave")
            sp.add_argument("--mode", choices=["overlay", "inplace"], help="sobrescreve translate.mode")
        if name == "terms":
            sp.add_argument("--min-count", type=_positive_int, default=5, help="mínimo de ocorrências (padrão 5)")
        if name in ("translate", "verify"):
            sp.add_argument("--mode", choices=["overlay", "inplace"], help="sobrescreve translate.mode")
        if name == "status":
            sp.add_argument("--backend", choices=["translators", "ollama", "openai", "google", "argos", "echo"],
                            help="calcula o que falta para ESTE backend")
        if name == "translate":
            sp.add_argument("--limit", type=_positive_int, help="traduz só os N primeiros textos de cada mod (teste)")
            sp.add_argument("--backend", choices=["translators", "ollama", "openai", "google", "argos", "echo"],
                            help="sobrescreve o backend da config ('echo' = teste sem IA)")
    for stream in (sys.stdout, sys.stderr):            # no Windows o console cp1252 quebraria com texto de outros alfabetos
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = ap.parse_args(argv)

    try:
        return _dispatch(args)
    except (ConfigError, BackendError, OSError, sqlite3.Error) as error:
        print(f"ERRO: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrompido.", file=sys.stderr)
        return 130


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "argos-setup":
        install_argos_package("en", "pt")
        print("Pacote en->pt instalado. Agora use  backend = \"argos\"  no config.toml.")
        return 0
    cfg = load_config(args.config)
    if getattr(args, "mode", None):
        cfg["translate"]["mode"] = args.mode
    mods = resolve_mods(cfg, args.mod)
    if args.cmd == "scan":
        return cmd_scan(cfg, mods)
    if args.cmd == "terms":
        return cmd_terms(cfg, mods, args.min_count)
    if args.cmd == "diff":
        return cmd_diff(cfg, mods, args.find)
    if args.cmd == "verify":
        return cmd_verify(cfg, mods)
    if args.cmd == "status":
        return cmd_status(cfg, mods, args.backend)
    if args.cmd == "package":
        return cmd_package(cfg, mods)
    return cmd_translate(cfg, mods, args.backend, args.limit)


if __name__ == "__main__":
    sys.exit(main())
