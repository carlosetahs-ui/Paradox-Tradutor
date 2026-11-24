"""Leitura, validação e resolução de caminhos da configuração TOML."""
from __future__ import annotations

import difflib
import math
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .ck2loc import decode_bytes
from .profiles import get_profile
from .termos import collect, read_glossary, read_keep

BACKENDS = ("translators", "ollama", "openai", "gemini", "google", "argos", "echo")
_KNOWN_TRANSLATE = {
    "source_column", "write_columns", "mode", "output_dir", "cache", "reuse_cache_from",
    "workers", "skip_files", "context", "safety", "keep_file", "glossary_file", "ptbr_fix", "replace_file",
}
_KNOWN_MODEL = {
    "backend", "name", "host", "temperature", "timeout", "api_key", "source_lang", "target_lang",
    "providers", "max_per_minute", "max_per_hour", "pause_min", "pause_max", "long_pause_every",
    "long_pause_min", "long_pause_max", "long_pause", "max_retries", "backoff_base", "cooldown", "max_chars",
    "batch_size",
}


class ConfigError(ValueError):
    """Configuração inválida com mensagem própria para o terminal."""


@dataclass
class Mod:
    name: str
    path: Path
    game: str = "ck2"

    @property
    def slug(self) -> str:
        name = unicodedata.normalize("NFKD", self.name).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "mod"

    @property
    def loc_dir(self) -> Path:
        return self.path / "localisation"


def _path(base: Path, value, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} deve ser um caminho não vazio entre aspas.")
    return (base / Path(value).expanduser()).resolve()


def _strings(value, key: str, *, nonempty: bool = False) -> None:
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not isinstance(item, str) or not item.strip() for item in value)):
        raise ConfigError(f"{key} deve ser uma lista de textos" + (" não vazia." if nonempty else "."))


def _number(section: dict, key: str, *, integer: bool = False, positive: bool = False) -> None:
    if key not in section:
        return
    value = section[key]
    expected = (int,) if integer else (int, float)
    if (type(value) not in expected or not math.isfinite(value)
            or (value <= 0 if positive else value < 0)):
        kind = "inteiro" if integer else "número"
        bound = "maior que zero" if positive else "maior ou igual a zero"
        raise ConfigError(f"{key} deve ser um {kind} {bound}.")


def warn_unknown_keys(cfg: dict) -> None:
    for key in cfg:
        if key not in {"game", "model", "translate", "mods", "glossary", "keep", "_base"}:
            print(f"AVISO: seção desconhecida [{key}] na config; será ignorada.")
    for section, known in (("game", {"id", "supported_version"}),
                           ("translate", _KNOWN_TRANSLATE), ("model", _KNOWN_MODEL)):
        for key in cfg.get(section, {}):
            if key not in known:
                near = difflib.get_close_matches(key, known, n=1)
                hint = f" Você quis dizer '{near[0]}'?" if near else ""
                print(f"AVISO: '{key}' em [{section}] não existe; será ignorado.{hint}")


def _validate(cfg: dict) -> None:
    for section in ("game", "model", "translate", "keep", "glossary"):
        if not isinstance(cfg.setdefault(section, {}), dict):
            raise ConfigError(f"[{section}] deve ser uma tabela TOML.")
    cfg["game"].setdefault("id", "ck2")
    try:
        profile = get_profile(cfg)
    except ValueError as error:
        raise ConfigError(str(error)) from error
    if "supported_version" in cfg["game"]:
        supported = cfg["game"]["supported_version"]
        if not isinstance(supported, str) or not supported.strip() or any(ord(c) < 32 for c in supported):
            raise ConfigError("game.supported_version deve ser um texto não vazio, sem caracteres de controle.")
    tr, model = cfg["translate"], cfg["model"]
    defaults = {
        "ptbr_fix": True, "source_column": "english", "write_columns": ["english"],
        "mode": "overlay", "workers": 1, "reuse_cache_from": "auto", "safety": "strict",
    }
    for key, value in defaults.items():
        tr.setdefault(key, value)
    _strings(tr["write_columns"], "translate.write_columns", nonempty=True)
    for column in [tr["source_column"], *tr["write_columns"]]:
        if not isinstance(column, str) or column not in profile.languages:
            raise ConfigError(f"Coluna/idioma desconhecido {column!r} para {profile.id}. Use: {', '.join(profile.languages)}")
    for key, options in (("mode", ("overlay", "inplace")), ("safety", ("normal", "strict", "paranoid"))):
        if tr[key] not in options:
            raise ConfigError(f"translate.{key} deve ser uma destas opções: {', '.join(options)}")
    if profile.id == "eu4":
        if tr["mode"] != "overlay":
            raise ConfigError("EU4 exige translate.mode = 'overlay' para gerar um mod de tradução separado.")
        if tr["write_columns"] != [tr["source_column"]]:
            raise ConfigError("EU4 exige write_columns = [source_column]: a tradução usa o mesmo idioma nativo "
                              "do arquivo original. Para PT-BR, use source_column = 'english' e write_columns = ['english'].")
    _number(tr, "workers", integer=True, positive=True)
    if type(tr["ptbr_fix"]) is not bool:
        raise ConfigError("translate.ptbr_fix deve ser true ou false, sem aspas.")
    if tr["reuse_cache_from"] != "auto":
        _strings(tr["reuse_cache_from"], "translate.reuse_cache_from (ou use 'auto')")
    _strings(tr.get("skip_files", []), "translate.skip_files")
    if not isinstance(tr.get("context", ""), str):
        raise ConfigError("translate.context deve ser um texto.")
    for key in ("keep_file", "glossary_file", "replace_file"):
        if key in tr and not isinstance(tr[key], str):
            _strings(tr[key], f"translate.{key}")
    _strings(cfg["keep"].get("terms", []), "keep.terms")
    for term, translation in cfg["glossary"].items():
        if (not term.strip() or not isinstance(translation, str) or not translation.strip()
                or any(c in translation for c in ";\r\n\t")):
            raise ConfigError(f"glossary.{term}: use uma tradução não vazia, sem ';' nem quebras de linha.")
    if model.get("backend", "ollama") not in BACKENDS:
        raise ConfigError(f"model.backend desconhecido. Use: {', '.join(BACKENDS)}")
    for key in ("name", "host", "api_key", "source_lang", "target_lang"):
        if key in model and not isinstance(model[key], str):
            raise ConfigError(f"model.{key} deve ser um texto.")
    for key in ("timeout",):
        _number(model, key, positive=True)
    for key in ("temperature", "pause_min", "pause_max", "long_pause_min", "long_pause_max",
                "long_pause", "backoff_base", "cooldown"):
        _number(model, key)
    for key in ("max_per_minute", "max_per_hour", "long_pause_every"):
        _number(model, key, integer=True)
    for key in ("max_retries", "max_chars", "batch_size"):
        _number(model, key, integer=True, positive=True)
    if model.get("batch_size", 1) > 50:
        raise ConfigError("model.batch_size não pode ser maior que 50.")
    backend = model.get("backend", "ollama")
    pause_min, pause_max, long_min, long_max = {
        "google": (1.0, 2.0, 40, 90),
        "gemini": (0, 0, 0, 0),
    }.get(backend, (1.5, 4.0, 40, 90))
    for low, high, default_low, default_high in (
        ("pause_min", "pause_max", pause_min, pause_max),
        ("long_pause_min", "long_pause_max", long_min, long_max),
    ):
        if model.get(low, default_low) > model.get(high, default_high):
            raise ConfigError(f"model.{low} não pode ser maior que model.{high}.")
    if "providers" in model:
        _strings(model["providers"], "model.providers", nonempty=True)
    if not isinstance(cfg.get("mods"), list) or not cfg["mods"]:
        raise ConfigError("Nenhum mod em [[mods]] na config. Liste os mods que deseja traduzir.")
    for entry in cfg["mods"]:
        if not isinstance(entry, dict):
            raise ConfigError("Cada [[mods]] deve ser uma tabela com path e, opcionalmente, name/mod_file.")
        _path(cfg["_base"], entry.get("path"), "mods.path")
        for key in ("name", "mod_file"):
            if key in entry and (not isinstance(entry[key], str) or not entry[key].strip()
                                 or any(ord(c) < 32 for c in entry[key])):
                raise ConfigError(f"mods.{key} deve ser um texto não vazio, sem caracteres de controle.")


def load_terms(cfg: dict, base: Path) -> None:
    tr = cfg["translate"]
    keep = list(cfg["keep"].get("terms", []))
    glossary = dict(cfg["glossary"])
    replacements = {}
    try:
        for path in collect(base, tr.get("keep_file"), "keep.txt"):
            keep.extend(read_keep(path))
        for path in collect(base, tr.get("glossary_file"), "glossary.txt"):
            glossary.update(read_glossary(path))
        for path in collect(base, tr.get("replace_file"), "substituicoes.txt"):
            replacements.update(read_glossary(path))
    except (OSError, UnicodeError, ValueError) as error:
        raise ConfigError(f"Erro no arquivo de termos/substituições: {error}") from error
    low = {key.lower() for key in glossary}
    cfg["keep_terms"] = list(dict.fromkeys(term for term in keep if term.lower() not in low))
    cfg["glossary_terms"] = glossary
    cfg["replacements"] = replacements


def load_config(path: str) -> dict:
    config_path = Path(path).expanduser().resolve()
    try:
        cfg = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise ConfigError(f"Config não encontrada: {config_path} "
                          "(copie examples/config.ck2.toml para config.toml, "
                          "ou examples/config.eu4.toml para config.eu4.toml na raiz)") from error
    except (tomllib.TOMLDecodeError, UnicodeError) as error:
        raise ConfigError(f"TOML inválido em {config_path}: {error}") from error
    base = cfg["_base"] = config_path.parent
    _validate(cfg)
    warn_unknown_keys(cfg)
    for key, default in (("output_dir", "saida"), ("cache", "cache.sqlite")):
        cfg["translate"][key] = _path(base, cfg["translate"].get(key, default), f"translate.{key}")
    load_terms(cfg, base)
    return cfg


def _read_mod_name(path: Path) -> str | None:
    try:
        text, _ = decode_bytes(path.read_bytes())
    except OSError:
        return None
    match = re.search(r'^\s*name\s*=\s*"((?:\\[^\r\n]|[^"\\\r\n])+)"', text, re.M)
    return re.sub(r'\\(["\\])', r'\1', match.group(1)) if match else None


def resolve_mods(cfg: dict, only: str | None) -> list[Mod]:
    profile = get_profile(cfg)
    if profile.id == "eu4" and cfg["translate"]["mode"] != "overlay":
        raise ConfigError("EU4 exige translate.mode = 'overlay' para gerar um mod de tradução separado.")
    mods = []
    original_paths = []
    for entry in cfg["mods"]:
        path = _path(cfg["_base"], entry["path"], "mods.path")
        if not (path / "localisation").is_dir():
            print(f"AVISO: '{path}' não tem pasta localisation; ignorado.")
            continue
        original_paths.append(path)
        name = entry.get("name")
        if not name and entry.get("mod_file"):
            name = _read_mod_name(_path(cfg["_base"], entry["mod_file"], "mods.mod_file"))
        if not name and profile.id == "eu4":
            name = _read_mod_name(path / "descriptor.mod")
        if not name:
            name = next((found for file in sorted(path.glob("*.mod")) if (found := _read_mod_name(file))), None)
        if not name:
            name = path.name
            print(f"AVISO: sem 'name' para '{path}'; usando '{name}'. Defina o nome EXATO do .mod original na config.")
        if not only or only.lower() in name.lower():
            mods.append(Mod(name, path, profile.id))
    if not mods:
        raise ConfigError("Nenhum mod selecionado. Confira mods.path e o filtro --mod.")
    slugs = set()
    for mod in mods:
        if mod.slug in slugs:
            raise ConfigError(f"Mods com o mesmo nome de saída '{mod.slug}'. Use nomes distintos ou --mod.")
        slugs.add(mod.slug)
        target = (cfg["translate"]["output_dir"] / f"{mod.slug}_ptbr").resolve()
        if cfg["translate"]["mode"] == "overlay" and any(
                target == original or target.is_relative_to(original)
                or original.is_relative_to(target) for original in original_paths):
            raise ConfigError(f"A saída de {mod.name} coincide ou se sobrepõe a um mod original. Mude translate.output_dir.")
    return mods
