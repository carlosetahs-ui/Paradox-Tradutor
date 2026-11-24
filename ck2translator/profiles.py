"""Perfis de formato, idiomas e descritores dos jogos suportados."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .files import write_if_changed

if TYPE_CHECKING:
    from .config import Mod


@dataclass(frozen=True)
class GameProfile:
    id: str
    name: str
    suffix: str
    recursive: bool
    languages: tuple[str, ...]
    prompt_context: str


PROFILES = {
    "ck2": GameProfile(
        "ck2", "Crusader Kings II", ".csv", False,
        ("english", "french", "german", "spanish"),
        "Europa medieval, dinastias, política",
    ),
    "eu4": GameProfile(
        "eu4", "Europa Universalis IV", ".yml", True,
        ("english", "french", "german", "spanish"),
        "Idade Moderna, diplomacia, comércio, exploração e impérios",
    ),
}


def get_profile(game: dict | str | None = None) -> GameProfile:
    """Obtém o perfil por id ou configuração; configurações antigas usam CK2."""
    if isinstance(game, dict):
        game = game.get("game", {}).get("id", "ck2")
    identifier = "ck2" if game is None else game
    try:
        return PROFILES[identifier]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Jogo desconhecido {identifier!r}. Use: {', '.join(PROFILES)}") from error


def _quoted(value: str) -> str:
    # O formato Clausewitz usa barras invertidas também como escapes.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_descriptors(out_root: Path, mod: Mod, cfg: dict | None = None) -> None:
    """Gera somente os metadados da tradução, sem copiar ids do Workshop."""
    profile = get_profile(cfg if cfg is not None else mod.game)
    folder = f"{mod.slug}_ptbr"
    if profile.id == "ck2":
        # Preserva bytes e convenções dos descritores gerados pelas versões antigas.
        safe = mod.name.replace('"', "'")
        text = (f'name = "{safe} [PT-BR]"\n'
                f'path = "mod/{folder}"\n'
                f'dependencies = {{ "{safe}" }}\n')
        write_if_changed(out_root / f"{folder}.mod", text.encode("cp1252", errors="replace"))
        return

    text = (f"name = {_quoted(mod.name + ' [PT-BR]')}\n"
            'tags = { "Translation" }\n'
            f"dependencies = {{ {_quoted(mod.name)} }}\n")
    supported = (cfg or {}).get("game", {}).get("supported_version")
    if supported:
        text += f"supported_version = {_quoted(supported)}\n"
    # O launcher reconstitui o arquivo externo a partir do descriptor interno.
    # O caminho é local à instalação e pertence apenas ao arquivo externo.
    write_if_changed(out_root / folder / "descriptor.mod", text.encode("utf-8"))
    external = text + f'path = "mod/{folder}"\n'
    write_if_changed(out_root / f"{folder}.mod", external.encode("utf-8"))
