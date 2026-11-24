#!/usr/bin/env python3
"""Tradutor de localisation de mods Paradox (CK2 e Europa Universalis IV).

  python ck2tr.py scan      --config config.toml     # só analisa 
  python ck2tr.py translate --config config.toml     # traduz os mods listados
  python ck2tr.py verify    --config config.toml     # confere original x traduzido

"""
from __future__ import annotations

import argparse
import collections
import csv
import fnmatch
import hashlib
import io
import json
import re
import shutil
import sqlite3
import sys
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import islice
from pathlib import Path, PurePosixPath
from threading import Event, Lock

from .ck2loc import EU4_TOKEN_RE, TOKEN_RE
from . import localisation, revisao
from .config import BACKENDS, ConfigError, Mod, load_config, resolve_mods
from .files import atomic_path, atomic_write, write_if_changed
from .profiles import get_profile, write_descriptors
from .quality import quality_problems, unchanged_prose
from . import __version__
from .translator import (PROMPT_VERSION, ArgosBackend, Backend, BackendError, Cache, EchoBackend, GeminiBackend,
                        GoogleBackend, OllamaBackend,
                        OpenAICompatBackend, RateLimiter, Result, TextBackendError, Translator, TranslatorsBackend,
                        fit_cp1252, install_argos_package, mask, normalize_punct, translation_cancel)


# --------------------------------------------------------------------------- configuração
def loc_files(mod: Mod, cfg: dict) -> list[Path]:
    skip = [s.lower() for s in cfg["translate"].get("skip_files", [])]
    profile = get_profile(cfg)
    paths = mod.loc_dir.rglob("*") if profile.recursive else mod.loc_dir.iterdir()
    return sorted(p for p in paths
                  if p.suffix.lower() == profile.suffix and p.is_file()
                  and not any(fnmatch.fnmatch(p.name.lower(), pat) or
                              fnmatch.fnmatch(p.relative_to(mod.loc_dir).as_posix().lower(), pat)
                              for pat in skip))


def _read_loc(path: Path, cfg: dict, *, data: bytes | None = None):
    try:
        game = get_profile(cfg).id
        loc = localisation.read(path, game, ignore_invalid=True, data=data)
    except (ValueError, UnicodeError) as error:
        raise ConfigError(f"Localisation inválida em {path}: {error}") from error
    if game == "eu4":
        for number, line in enumerate(loc.lines, 1):
            if line.invalid_reason:
                print(f"AVISO: Localisation inválida em {path}: linha {number} ignorada na tradução "
                      f"e preservada sem alterações: {line.invalid_reason}", file=sys.stderr)
    return loc


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
    elif kind == "gemini":
        limiter = RateLimiter(int(m.get("max_per_minute", 10)), int(m.get("max_per_hour", 0)),
                              (float(m.get("pause_min", 0)), float(m.get("pause_max", 0))),
                              int(m.get("long_pause_every", 0)),
                              (float(m.get("long_pause_min", 0)), float(m.get("long_pause_max", 0))))
    if offline:
        backend = Backend()
        model = m.get("name", "")
        if kind == "gemini":
            canonical = GeminiBackend.model_name(model)
            if canonical != model:
                backend.legacy_model_id = f"gemini:{model}"
            model = canonical
        backend.model_id = f"{kind}:{model}" if kind in ("ollama", "openai", "gemini") else kind
        backend.uses_prompt = kind in ("ollama", "openai", "gemini", "echo")
        backend.limiter = limiter
        backend.batch_size = int(m.get("batch_size", 1)) if kind == "gemini" else 1
        return backend
    if kind == "echo":
        return EchoBackend()
    if kind == "translators":
        return TranslatorsBackend(m.get("providers", ["google", "bing", "alibaba", "yandex"]),
                                  m.get("source_lang", "en"), m.get("target_lang", "pt"), limiter,
                                  int(m.get("max_retries", 4)), float(m.get("backoff_base", 15)),
                                  float(m.get("cooldown", 600)), int(m.get("max_chars", 3000)),
                                  timeout=float(m.get("timeout", 30)))
    if kind == "google":
        return GoogleBackend(m.get("source_lang", "en"), m.get("target_lang", "pt"),
                             (float(m.get("pause_min", 1.0)), float(m.get("pause_max", 2.0))),
                             int(m.get("long_pause_every", 100)), float(m.get("long_pause", 30)),
                             timeout=float(m.get("timeout", 30)))
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
    if kind == "gemini":
        return GeminiBackend(name, m.get("api_key", ""), temp, timeout, limiter,
                             int(m.get("batch_size", 1)))
    raise ConfigError(f"backend desconhecido: {kind}")


# --------------------------------------------------------------------------- scan
def cmd_scan(cfg: dict, mods: list[Mod]) -> int:
    tr = cfg["translate"]
    src_name = tr["source_column"]
    ck2 = get_profile(cfg).id == "ck2"
    seen_in: dict[str, list[str]] = {}
    for mod in mods:
        print(f"\n== {mod.name}  ({mod.path})")
        uniq: set[str] = set()
        total_chars = total_entries = 0
        for p in loc_files(mod, cfg):
            name = p.relative_to(mod.loc_dir).as_posix()
            seen_in.setdefault(name.lower(), []).append(mod.name)
            loc = _read_loc(p, cfg)
            n_entries = odd = 0
            for line in loc.entries():
                n_entries += 1
                if ck2 and len(line.fields) != 15:
                    odd += 1
            n_src = 0
            for _, text in localisation.source_entries(loc, src_name):
                n_src += 1
                uniq.add(text)
                total_chars += len(text)
            total_entries += n_src
            flag = f"  [{odd} linhas com nº de colunas != 15]" if odd else ""
            print(f"  {name:<45} {loc.encoding:<9} {n_entries:>6} entradas, "
                  f"{n_src:>6} com texto em '{src_name}'{flag}")
        print(f"  -> {total_entries} textos ({len(uniq)} únicos), ~{total_chars:,} caracteres")
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
        per = (time.monotonic() - t_model) / calls
        eta = f" | {per:.1f}s/texto, faltam até ~{_fmt_time(per * (total - done))}"
    sys.stdout.write(f"\r  traduzindo {done}/{total}  ok={counts['ok']} cache={counts['cache']} "
                     f"pulados={counts['skip']} falhas={counts['falha']}{eta}      ")
    sys.stdout.flush()


_MAX_CONSECUTIVE_BACKEND_FAILURES = 5


def _translate_batch(translator: Translator, todo: list[str], workers: int, *, consecutive: int = 0):
    stop = Event()
    texts = iter(todo)
    results = {}
    counts = {"ok": 0, "cache": 0, "skip": 0, "falha": 0}
    errors = 0
    t_model, last_print = None, 0.0

    def translate(text: str) -> Result:
        token = translation_cancel.set(stop)
        try:
            return translator.translate(text)
        finally:
            translation_cancel.reset(token)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(translate, text): text for text in islice(texts, workers)}
        try:
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    text = pending.pop(future)
                    try:
                        result = future.result()
                    except TextBackendError as error:
                        errors += 1
                        consecutive += 1
                        result = Result("falha", text, str(error))
                    else:
                        if result.status in ("ok", "falha"):
                            consecutive = 0
                    results[text] = result
                    counts[result.status] += 1
                if t_model is None and (counts["ok"] or counts["falha"]):
                    t_model = time.monotonic()
                now = time.monotonic()
                if len(results) == len(todo) or now - last_print >= 2:
                    _progress(len(results), len(todo), counts, t_model, counts["ok"] + counts["falha"])
                    last_print = now
                if consecutive >= _MAX_CONSECUTIVE_BACKEND_FAILURES:
                    break
                for text in islice(texts, len(done)):
                    pending[pool.submit(translate, text)] = text
        finally:
            stop.set()
            for future in pending:
                future.cancel()
    return results, counts, errors, consecutive


def translate_mod(mod: Mod, cfg: dict, translator: Translator, limit: int | None) -> int:
    tr = cfg["translate"]
    out_root: Path = tr["output_dir"]
    if tr["mode"] == "overlay":
        return _translate_overlay(mod, cfg, translator, limit)

    backup = out_root / "backup" / mod.slug
    if backup.exists():
        sys.exit(f"Já existe backup em {backup}. Este mod provavelmente já foi traduzido "
                 "in-place; traduzir de novo traduziria o português. Restaure o backup ou use overlay.")
    locs = [_read_loc(p, cfg) for p in loc_files(mod, cfg)]
    texts = dict.fromkeys(text for loc in locs for _, text in localisation.source_entries(loc, tr["source_column"]))
    todo = list(texts)[:limit] if limit is not None else list(texts)
    print(f"\n== {mod.name}: {len(locs)} arquivos, {len(todo)} textos únicos para traduzir")

    results, counts, _, _ = _translate_batch(translator, todo, tr["workers"])
    print()
    deferred = len(todo) - len(results)
    if deferred:
        print(f"  O backend falhou repetidamente. {deferred} texto(s) ficaram para a próxima execução.")
    if counts["falha"]:
        print("  O mod original foi preservado. Rode novamente para retomar pelo cache.")

    report: list[list[str]] = []
    for loc in locs:
        relative = loc.path.relative_to(mod.loc_dir)
        changed = _apply_translations(loc, relative.as_posix(), cfg, results, report)
        if changed and not counts["falha"]:
            backup_file = backup / "localisation" / relative
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(loc.path, backup_file)
            loc.write(loc.path)
            print(f"  gravado: {loc.path}  ({changed} linhas)")
    _write_report(out_root, mod, report)
    print(f"  resumo: {counts}")
    return len(report)


def _apply_translations(loc, name: str, cfg: dict, results: dict[str, Result], report: list[list[str]],
                        *, source_loc=None) -> int:
    tr = cfg["translate"]
    game = get_profile(cfg).id
    changed = 0
    original = loc if source_loc is None else source_loc
    for line, source in localisation.source_entries(original, tr["source_column"], destination=loc):
        res = results.get(source)
        if res is None or res.status == "skip":
            continue
        if res.status == "falha":
            report.append([name, line.key, "falha", source, res.note])
            continue
        new = fit_cp1252(res.text) if not loc.encoding.startswith("utf-8") else res.text
        if tr["safety"] != "normal" or game == "eu4":
            new = normalize_punct(source, new)
            bad = localisation.hard_problems(source, new, game)
            if bad:
                report.append([name, line.key, "revertida", source, "; ".join(bad)])
                continue
        try:
            localisation.set_translation(loc, line, tr["write_columns"], new)
        except ValueError as error:
            report.append([name, line.key, "revertida", source, str(error)])
            continue
        changed += 1
    return changed


def _write_report(out_root: Path, mod: Mod, report: list[list[str]]) -> None:
    rep = out_root / f"{mod.slug}_relatorio.csv"
    if report or rep.exists():
        with io.StringIO(newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["arquivo", "chave", "status", "original", "motivo"])
            writer.writerows(report)
            write_if_changed(rep, f.getvalue().encode("utf-8-sig"))
        if report:
            print(f"  {len(report)} linhas ficaram em inglês (veja {rep})")
    reverted = sum(row[2] == "revertida" for row in report)
    if reverted:
        print(f"  {reverted} linha(s) voltaram ao inglês pela trava de segurança (veja o relatório)")


def _overlay_policy(cfg: dict, translator: Translator) -> bytes:
    tr, model = cfg["translate"], cfg["model"]
    fields = ("source_column", "write_columns", "safety", "context", "ptbr_fix", "reuse_cache_from")
    settings = [tr.get(key) for key in fields]
    model_settings = [model.get(key) for key in ("host", "source_lang", "target_lang", "temperature", "providers")]
    values = [__version__, PROMPT_VERSION, get_profile(cfg).id, translator.backend.model_id, model_settings,
              settings, cfg["keep_terms"], list(cfg["glossary_terms"].items()), list(cfg["replacements"].items())]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode("ascii")).digest()


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _output_stamp(fingerprint: bytes, output: bytes) -> str:
    return hashlib.sha256(fingerprint + hashlib.sha256(output).digest()).hexdigest()


def _translate_overlay(mod: Mod, cfg: dict, translator: Translator, limit: int | None) -> int:
    tr = cfg["translate"]
    directory = _translation_directory(cfg, mod)
    files, completed, partial = _read_manifest(cfg, directory)
    recorded_files = files.copy()
    files.update(p.relative_to(directory).as_posix() for p in _generated_files(cfg, directory))
    policy = _overlay_policy(cfg, translator)
    sources = loc_files(mod, cfg)
    print(f"\n== {mod.name}: {len(sources)} arquivos para traduzir")

    counts = collections.Counter({"ok": 0, "cache": 0, "skip": 0, "falha": 0})
    report: list[list[str]] = []
    selected: set[str] = set()
    failed_texts: dict[str, Result] = {}
    consecutive = reused = 0
    descriptors_written = False

    for index, path in enumerate(sources, 1):
        relative = path.relative_to(mod.loc_dir)
        name = relative.as_posix()
        target = directory / relative
        if not target.resolve().is_relative_to(directory.parent.resolve()):
            raise ConfigError(f"Arquivo de saída fora da pasta do mod traduzido: {target}")
        data = path.read_bytes()
        # O registro só vale para estes bytes de origem, esta configuração e
        # os mesmos bytes de destino que foram gravados com sucesso.
        fingerprint = policy + hashlib.sha256(data).digest()
        previous = target.read_bytes() if target.is_file() else None
        stamp = _output_stamp(fingerprint, previous) if previous is not None else None
        if limit is None and stamp is not None and completed.get(name) == stamp:
            reused += 1
            continue

        original = _read_loc(path, cfg, data=data)
        candidates = dict.fromkeys(text for _, text in localisation.source_entries(original, tr["source_column"]))
        checkpoint = partial.get(name)
        resumed = limit is None and checkpoint is not None and checkpoint["stamp"] == stamp
        if resumed:
            pending_hashes = set(checkpoint["texts"])
            candidates = {text: None for text in candidates if _text_digest(text) in pending_hashes}
        todo = []
        results = {}
        for text in candidates:
            if limit is not None and text not in selected:
                if len(selected) >= limit:
                    continue
                selected.add(text)
            if text in failed_texts:
                results[text] = failed_texts[text]
            else:
                todo.append(text)

        if limit is not None and not todo and not results:
            continue
        if todo:
            print(f"  [{index}/{len(sources)}] {name}")
            batch, file_counts, _, consecutive = _translate_batch(
                translator, todo, tr["workers"], consecutive=consecutive)
            print()
            results.update(batch)
            failed_texts.update((text, result) for text, result in batch.items() if result.status == "falha")
            counts.update(file_counts)
        loc = original
        if resumed and any(result.status in ("ok", "cache") for result in results.values()):
            loc = localisation.read(target, get_profile(cfg).id, ignore_invalid=True, data=previous)
            loc.encoding = original.encoding
        before = len(report)
        changed = _apply_translations(loc, name, cfg, results, report, source_loc=original)
        if changed or previous is not None:
            if not descriptors_written:
                write_descriptors(tr["output_dir"], mod, cfg)
                descriptors_written = True
            output = previous if resumed and not changed else loc.to_bytes()
            if output != previous:
                atomic_write(target, output)
                print(f"  gravado: {target}  ({changed} linhas)")

            pending = set(candidates) - results.keys()
            pending.update(row[3] for row in report[before:])
            if output != previous:
                stamp = _output_stamp(fingerprint, output)
            complete = stamp if limit is None and not pending else None
            saved_partial = {"stamp": stamp, "texts": sorted(_text_digest(text) for text in pending)} if pending else None
            files.add(name)
            if (name not in recorded_files or completed.get(name) != complete
                    or checkpoint != saved_partial):
                completed.pop(name, None)
                partial.pop(name, None)
                if complete is not None:
                    completed[name] = complete
                if saved_partial is not None:
                    partial[name] = saved_partial
                _write_manifest(cfg, directory, files, completed, partial)
                recorded_files.update(files)
        if consecutive >= _MAX_CONSECUTIVE_BACKEND_FAILURES:
            deferred = len(todo) - len(batch)
            print(f"  O backend falhou repetidamente. No arquivo atual, {deferred} texto(s) "
                  "ficaram para a próxima execução.")
            if index < len(sources):
                print(f"  {len(sources) - index} arquivo(s) seguintes ficaram para a próxima execução.")
            break

    if reused and not descriptors_written:
        write_descriptors(tr["output_dir"], mod, cfg)
    if reused:
        print(f"  {reused} arquivo(s) concluído(s) reaproveitado(s) da saída.")
    _write_report(tr["output_dir"], mod, report)
    print(f"  resumo: {dict(counts)}")
    return len(report)


def write_mod_descriptor(out_root: Path, mod: Mod) -> None:
    """Entrada legada para a geração de descritores do CK2."""
    write_descriptors(out_root, mod)


class _LazyBackend(Backend):
    """Usa os metadados do backend até a primeira tradução que precisar dele."""

    def __init__(self, metadata: Backend, factory: Callable[[], Backend]):
        self.model_id = metadata.model_id
        self.legacy_model_id = metadata.legacy_model_id
        self.uses_prompt = metadata.uses_prompt
        self.batch_size = getattr(metadata, "batch_size", 1)
        self._factory = factory
        self._backend: Backend | None = None
        self._lock = Lock()

    def chat(self, system: str, user: str) -> str:
        with self._lock:
            if self._backend is None:
                self._backend = self._factory()
            backend = self._backend
        return backend.chat(system, user)

    @property
    def stats(self):
        return getattr(self._backend, "stats", None)


def make_translator(cfg: dict, backend_override: str | None, *, offline: bool = False):
    backend = build_backend(cfg, backend_override, offline=True)
    if not offline:
        backend = _LazyBackend(backend, lambda: build_backend(cfg, backend_override))
    cache = Cache(str(cfg["translate"]["cache"]), read_only=offline)
    name = cfg["model"].get("name", "")
    rc = cfg["translate"]["reuse_cache_from"]
    reuse = rc
    if rc != "auto":
        reuse = []
        for entry in rc:
            model_id = f"{entry}:{name}" if entry in ("ollama", "openai", "gemini") else entry
            if model_id.startswith("gemini:"):
                canonical = GeminiBackend.model_name(model_id.split(":", 1)[1])
                reuse.append(f"gemini:{canonical}")
            reuse.append(model_id)
        reuse = list(dict.fromkeys(reuse))
    post = revisao.build(bool(cfg["translate"]["ptbr_fix"]), cfg.get("replacements"))
    translator = Translator(backend, cache, cfg["glossary_terms"], cfg["keep_terms"],
                            cfg["translate"].get("context", ""), reuse_from=reuse,
                            paranoid=cfg["translate"]["safety"] == "paranoid", post=post,
                            game=get_profile(cfg).id)
    return backend, cache, translator


def cmd_translate(cfg: dict, mods: list[Mod], backend_override: str | None, limit: int | None) -> int:
    # Confere todos os overlays antes de abrir cache, iniciar serviços ou
    # gravar saídas, inclusive quando dois perfis compartilham o mesmo nome.
    if cfg["translate"]["mode"] == "overlay":
        for mod in mods:
            _read_manifest(cfg, _translation_directory(cfg, mod))
    backend, cache, translator = make_translator(cfg, backend_override)
    try:
        print(f"Modelo: {backend.model_id}  | modo: {cfg['translate']['mode']}  | segurança: {cfg['translate']['safety']}")
        batch_size = getattr(backend, "batch_size", 1)
        if batch_size > 1:
            effective = min(batch_size, cfg["translate"]["workers"])
            print(f"Lotes: até {effective} textos por pedido Gemini "
                  f"(batch_size={batch_size}, workers={cfg['translate']['workers']})")
        if backend.model_id == "echo":
            print("Modo de teste echo: acrescenta 'PT> ' aos textos; não realiza tradução.")
        print(f"Termos: {len(cfg['keep_terms'])} para manter, {len(cfg['glossary_terms'])} no glossário")
        if cfg["replacements"] or cfg["translate"]["ptbr_fix"]:
            print(f"Pós-edição: ptbr_fix={'sim' if cfg['translate']['ptbr_fix'] else 'não'}, "
                  f"{len(cfg['replacements'])} substituições suas")
        failures = sum(translate_mod(m, cfg, translator, limit) for m in mods)
        if translator.reused:
            ids = ", ".join(translator.reuse_ids())
            print(f"Textos reaproveitados de outros backends: {translator.reused} ({ids})")
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
          f"da saída para a pasta mod de {get_profile(cfg).name}.")
    return 1 if failures else 0


# --------------------------------------------------------------------------- status
def _est_seconds_per_text(backend, workers: int) -> float | None:
    """Estimativa de segundos por texto para backends com limitador (translators/google/gemini)."""
    lim = getattr(backend, "limiter", None)
    if lim is None:
        return None
    per = sum(lim.pause) / 2 + (sum(lim.long_pause) / 2 / lim.long_every if lim.long_every else 0)
    request_time = max(per, 60 / lim.per_minute if lim.per_minute else 0,
                       3600 / lim.per_hour if lim.per_hour else 0)
    return request_time / min(workers, getattr(backend, "batch_size", 1))


def cmd_status(cfg: dict, mods: list[Mod], backend_override: str | None) -> int:
    """Quanto já está no cache e quanto falta, SEM chamar nenhum serviço de tradução."""
    backend, cache, translator = make_translator(cfg, backend_override, offline=True)
    src_name = cfg["translate"]["source_column"]
    try:
        total_pend = 0
        for mod in mods:
            texts: dict[str, None] = {}
            for p in loc_files(mod, cfg):
                loc = _read_loc(p, cfg)
                for _, text in localisation.source_entries(loc, src_name):
                    texts.setdefault(text, None)
            c = collections.Counter(translator.translate(t, dry=True).status for t in texts)
            pend = c["pendente"]
            total_pend += pend
            done = c["cache"] + c["ok"]
            pct = 100 * (done + c["skip"]) / max(1, len(texts))
            print(f"{mod.name}: {len(texts)} textos únicos | prontos {done} | pulados {c['skip']} | "
                  f"faltam {pend}  ({pct:.0f}% resolvido)")
        per = _est_seconds_per_text(backend, cfg["translate"]["workers"])
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
1. Copie a pasta "{slug}_ptbr" e o arquivo "{slug}_ptbr.mod" para a pasta de mods de {game}:
   Documentos\\Paradox Interactive\\{game}\\mod
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
        if get_profile(cfg).id == "eu4" and not (folder / "descriptor.mod").is_file():
            print(f"{mod.name}: falta descriptor.mod interno; rode 'translate' novamente.")
            failures += 1
            continue
        if cmd_verify(cfg, [mod]):
            print(f"{mod.name}: pacote não gerado; corrija os problemas encontrados pelo verify.")
            failures += 1
            continue
        zpath = out_root / f"{mod.slug}_ptbr.zip"
        languages = {"english": "INGLÊS", "french": "FRANCÊS", "german": "ALEMÃO", "spanish": "ESPANHOL",
                     "russian": "RUSSO"}
        readme = LEIA_ME.format(name=mod.name, slug=mod.slug,
                               game=get_profile(cfg).name,
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


def suggest_terms(texts: list[str], min_count: int = 5, *, game: str = "ck2") -> list[tuple[str, int]]:
    """Acha nomes próprios: palavras que aparecem com inicial maiúscula no MEIO das frases e quase nunca em minúscula."""
    cap_mid: collections.Counter = collections.Counter()
    lower: collections.Counter = collections.Counter()
    toks_all = []
    token_re = EU4_TOKEN_RE if game == "eu4" else TOKEN_RE
    for t in texts:
        toks = _TOK_RE.findall(mask(t, token_re=token_re)[0].replace("\\n", "\n"))
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
            loc = _read_loc(p, cfg)
            seen.update(text for _, text in localisation.source_entries(loc, src_name))
    found = suggest_terms(sorted(seen), min_count, game=get_profile(cfg).id)
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


_MANIFEST_NAME = "translation_manifest.json"


def _translation_directory(cfg: dict, mod: Mod) -> Path:
    tr = cfg["translate"]
    if tr["mode"] == "overlay":
        return tr["output_dir"] / f"{mod.slug}_ptbr" / "localisation"
    return tr["output_dir"] / "backup" / mod.slug / "localisation"


def _generated_files(cfg: dict, directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    profile = get_profile(cfg)
    paths = directory.rglob("*") if profile.recursive else directory.iterdir()
    return sorted(p for p in paths if p.is_file() and p.suffix.lower() == profile.suffix)


def _manifest_hash(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _read_manifest(cfg: dict, directory: Path) -> tuple[set[str], dict[str, str], dict[str, dict]]:
    manifest = directory.parent / _MANIFEST_NAME
    if not manifest.exists():
        return set(), {}, {}  # Traduções antigas continuam verificáveis.
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("game") != get_profile(cfg).id or not isinstance(data.get("files"), list)):
            raise ValueError("versão, perfil ou lista de arquivos inválidos")
        for name in data["files"]:
            if (not isinstance(name, str) or not name or "\\" in name or ":" in name
                    or any(ord(char) < 32 for char in name) or PurePosixPath(name).is_absolute()
                    or any(part in ("", ".", "..") for part in name.split("/"))
                    or PurePosixPath(name).suffix.lower() != get_profile(cfg).suffix):
                raise ValueError("caminho de arquivo inválido")
        files = set(data["files"])
        completed = data.get("completed", {})
        if (not isinstance(completed, dict) or any(name not in files or not _manifest_hash(stamp)
                                                   for name, stamp in completed.items())):
            raise ValueError("registro de arquivos concluídos inválido")
        partial = data.get("partial", {})
        if not isinstance(partial, dict):
            raise ValueError("registro de arquivos pendentes inválido")
        for name, checkpoint in partial.items():
            if (name not in files or name in completed or not isinstance(checkpoint, dict)
                    or not _manifest_hash(checkpoint.get("stamp"))
                    or not isinstance(checkpoint.get("texts"), list) or not checkpoint["texts"]
                    or any(not _manifest_hash(item) for item in checkpoint["texts"])):
                raise ValueError("registro de arquivos pendentes inválido")
        return files, completed, partial
    except (ValueError, UnicodeError) as error:
        raise ConfigError(f"Manifesto inválido em {manifest}: {error}") from error


def _write_manifest(cfg: dict, directory: Path, files: set[str], completed: dict[str, str],
                    partial: dict[str, dict]) -> None:
    data = {"version": 1, "game": get_profile(cfg).id, "files": sorted(files), "completed": completed,
            "partial": partial}
    atomic_write(directory.parent / _MANIFEST_NAME,
                 (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _translation_pairs(cfg: dict, mod: Mod) -> list[tuple[Path, Path]]:
    tr = cfg["translate"]
    directory = _translation_directory(cfg, mod)
    files = set(_generated_files(cfg, directory))
    if tr["mode"] == "overlay":
        expected, _, _ = _read_manifest(cfg, directory)
        files.update(directory / name for name in expected)
    files = sorted(files)
    return ([(mod.loc_dir / p.relative_to(directory), p) for p in files] if tr["mode"] == "overlay"
            else [(p, mod.loc_dir / p.relative_to(directory)) for p in files])


def cmd_diff(cfg: dict, mods: list[Mod], find: str | None) -> int:
    """Mostra original x traduzido. Sem --find: só as linhas com algum aviso. Com --find: todas as que contêm o texto."""
    tr = cfg["translate"]
    game = get_profile(cfg).id
    token_re = EU4_TOKEN_RE if game == "eu4" else TOKEN_RE
    query = find.lower() if find else None
    src_col, shown, total_warn = tr["source_column"], 0, 0
    for mod in mods:
        for src_path, dst_path in _translation_pairs(cfg, mod):
            if not src_path.exists() or not dst_path.exists():
                continue
            src, dst = _read_loc(src_path, cfg), _read_loc(dst_path, cfg)
            head = f"\n=== {mod.name} / {dst_path.name}  (encoding original: {src.encoding}, traduzido: {dst.encoding})"
            printed_head = False
            if len(src.lines) != len(dst.lines):
                print(head + f"\n  ! número de linhas difere: {len(src.lines)} -> {len(dst.lines)}")
                continue
            for n, (a, b) in enumerate(zip(src.lines, dst.lines), start=1):
                if not (a.is_entry and b.is_entry):
                    continue
                ta = localisation.cell(src, a, src_col)
                if ta is None:
                    continue
                for column in tr["write_columns"]:
                    tb = localisation.cell(dst, b, column)
                    if tb is None:
                        continue
                    if query and not any(query in value.lower() for value in (a.key, ta, tb)):
                        continue
                    unchanged = localisation.cell(src, a, column) == tb
                    warns = [] if unchanged else struct_warnings(ta, tb)
                    if not unchanged or ta == tb:
                        warns.extend(quality_problems(ta, tb))
                    if unchanged_prose(ta, tb, keep=cfg.get("keep_terms", []),
                                       glossary=cfg.get("glossary_terms", {}), token_re=token_re):
                        warns.append("prosa em inglês ficou igual ao original; confira se faltou traduzir")
                    if game == "eu4" and not unchanged:
                        warns.extend(w for w in localisation.hard_problems(ta, tb, game) if w not in warns)
                    if not query and not warns:
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
            try:
                src, dst = _read_loc(src_path, cfg), _read_loc(dst_path, cfg)
                probs = localisation.compare(src, dst, tr["source_column"], tr["write_columns"])
            except ConfigError as error:
                probs = [str(error)]
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
    ap = argparse.ArgumentParser(description="Tradutor de mods Paradox (CK2 e EU4) para português do Brasil.")
    ap.add_argument("--version", action="version", version=f"CK2 Tradutor {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("argos-setup", help="baixa o pacote de idiomas en->pt do Argos (uma vez só, precisa de internet)")
    for name in ("scan", "translate", "verify", "terms", "diff", "status", "package"):
        if name == "verify":
            sp = sub.add_parser(name, aliases=["check"], help="verifica os arquivos traduzidos (também: check)")
        else:
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
            sp.add_argument("--backend", choices=BACKENDS,
                            help="calcula o que falta para ESTE backend")
        if name == "translate":
            sp.add_argument("--limit", type=_positive_int, help="traduz só os N primeiros textos de cada mod (teste)")
            sp.add_argument("--backend", choices=BACKENDS,
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
    if args.cmd in ("verify", "check"):
        return cmd_verify(cfg, mods)
    if args.cmd == "status":
        return cmd_status(cfg, mods, args.backend)
    if args.cmd == "package":
        return cmd_package(cfg, mods)
    return cmd_translate(cfg, mods, args.backend, args.limit)


if __name__ == "__main__":
    sys.exit(main())
