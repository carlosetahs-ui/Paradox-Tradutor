"""Tradução de jogos Paradox: proteção dos códigos, cache e glossário.

Só usa a biblioteca padrão (urllib, sqlite3), então não precisa de pip install.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import random
import re
import sqlite3
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import CancelledError
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from .ck2loc import EU4_TOKEN_RE, STRUCT_CHARS, TOKEN_RE, has_meaningful_text
from .profiles import get_profile
from .quality import quality_problems, unchanged_prose

PROMPT_VERSION = "v1"          # mude se alterar o prompt: invalida o cache antigo
PH_RE = re.compile(r"\{(\d+)\}")
# Alfabetos que nunca devem aparecer numa tradução para português (modelos pequenos às vezes "vazam" chinês etc.)
FOREIGN_RE = re.compile("[\u0400-\u04ff\u0590-\u06ff\u0e00-\u0e7f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
NON_PROMPT_IDS = {"google", "argos", "translators"}   # backends que mascaram também os termos [keep] (ver Translator.translate)


class BackendError(RuntimeError):
    """Falha de backend que exige parar a execução."""


class TextBackendError(BackendError):
    """Falha que permite registrar o texto e continuar o lote."""


translation_cancel: ContextVar[threading.Event | None] = ContextVar("translation_cancel", default=None)


def _check_cancelled() -> None:
    stop = translation_cancel.get()
    if stop is not None and stop.is_set():
        raise CancelledError()


def _sleep(seconds: float) -> None:
    stop = translation_cancel.get()
    if stop is None:
        time.sleep(seconds)
    elif stop.wait(seconds):
        raise CancelledError()


# --------------------------------------------------------------------------- códigos do jogo
def mask(text: str, extra_terms: list[str] | None = None,
         glossary: list[tuple[str, str]] | None = None, *,
         token_re: re.Pattern[str] = TOKEN_RE) -> tuple[str, list[str]]:
    """Troca cada código do jogo por {1}, {2}... e devolve a lista do que entra no lugar de cada marcador.

    extra_terms: termos [keep] (voltam exatamente como estavam).
    glossary:    pares (termo, tradução) (no lugar do marcador entra a TRADUÇÃO do glossário,
                 com a inicial maiúscula se o termo apareceu com inicial maiúscula).
    Usado por backends sem prompt (translators, google, argos), que não entendem instruções."""
    tokens: list[str] = []
    regex = token_re
    forced = {k.lower(): v for k, v in (glossary or [])}
    names = sorted({*(extra_terms or []), *forced}, key=len, reverse=True)
    term_regex = None
    if names:
        terms = "|".join(re.escape(t) for t in names)
        term_pattern = r"(?<![\w])(?:" + terms + r")(?![\w])"
        term_regex = re.compile(term_pattern, re.I)
        regex = re.compile(token_re.pattern + "|" + term_pattern, re.I)

    def repl(m: re.Match, *, term: bool = True) -> str:
        found = m.group(0)
        value = forced.get(found.lower()) if term else None
        if value is not None:
            if found[:1].isupper() and value[:1].islower():
                value = value[:1].upper() + value[1:]
            found = value
        tokens.append(found)
        return "{%d}" % len(tokens)

    if term_regex is not None:
        # As letras em §Y e \\n não fazem parte do texto visível. Procurar os
        # termos somente entre códigos também impede casar dentro de $VAR$.
        parts = []
        position = 0
        for code in token_re.finditer(text):
            parts.append(term_regex.sub(repl, text[position:code.start()]))
            parts.append(repl(code, term=False))
            position = code.end()
        parts.append(term_regex.sub(repl, text[position:]))
        return "".join(parts), tokens

    return regex.sub(repl, text), tokens


def unmask(text: str, tokens: list[str]) -> str:
    return PH_RE.sub(lambda m: tokens[int(m.group(1)) - 1], text)


def has_letters(masked: str) -> bool:
    return any(ch.isalpha() for ch in PH_RE.sub("", masked))


def placeholders_problem(out: str, n: int) -> str:
    found = sorted(int(x) for x in PH_RE.findall(out))
    if found != list(range(1, n + 1)):
        return "perdeu, repetiu ou inventou marcadores {n}"
    if "{" in PH_RE.sub("", out) or "}" in PH_RE.sub("", out):
        return "chaves { } soltas na resposta"
    return ""


_TYPO = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2026": "...", "\u2013": "-", "\u2014": "-"}
_TYPO.update({"«": '"', "»": '"', "‹": "'", "›": "'"})


def normalize_punct(src: str, dst: str) -> str:
    """Troca aspas/travessões/reticências "tipográficos" por ASCII, SE o texto original não os usava."""
    replacements = _TYPO
    if src.count("'") >= 2 and '"' not in src:
        replacements = {**_TYPO, "«": "'", "»": "'"}
    return "".join(replacements[c] if c in replacements and c not in src else c for c in dst)


def restore_marker_spaces(source: str, translated: str) -> str:
    """Recupera separadores perdidos junto a marcadores, sem separar cores de seu texto."""
    edges = {m.group(0): (m.start() > 0 and source[m.start() - 1].isspace(),
                           m.end() < len(source) and source[m.end()].isspace())
             for m in PH_RE.finditer(source)}

    def repair(match: re.Match) -> str:
        before, after = edges.get(match.group(0), (False, False))
        left = translated[match.start() - 1:match.start()] if match.start() else ""
        right = translated[match.end():match.end() + 1]
        return ((" " if before and left.isalnum() else "")
                + match.group(0)
                + (" " if after and (right.isalnum() or right == "{") else ""))
    return PH_RE.sub(repair, translated)


def color_markers_problem(source: str, translated: str, tokens: list[str]) -> str:
    """Preserva a ordem e os trechos visíveis das cores, inclusive resets §W legados."""
    colors = {str(i) for i, token in enumerate(tokens, 1)
              if re.fullmatch(r"§[A-Za-z!]", token)}
    original = [m for m in PH_RE.finditer(source) if m.group(1) in colors]
    output = [m for m in PH_RE.finditer(translated) if m.group(1) in colors]
    if [m.group(1) for m in original] != [m.group(1) for m in output]:
        return "a ordem dos códigos de cor mudou"
    for index, (start, restored) in enumerate(zip(original, output)):
        if tokens[int(start.group(1)) - 1] == "§!":
            continue
        end = original[index + 1].start() if index + 1 < len(original) else len(source)
        restored_end = output[index + 1].start() if index + 1 < len(output) else len(translated)
        if source[start.end():end].strip() and not translated[restored.end():restored_end].strip():
            return "o texto de um trecho colorido desapareceu"
        if (has_meaningful_text(source[start.end():end])
                and not has_meaningful_text(translated[restored.end():restored_end])):
            return "o texto de um trecho colorido desapareceu; restaram somente marcadores"
    return ""


# --------------------------------------------------------------------------- encoding
_SUBS = {"→": "->", "←": "<-", "•": "*", " ": " ", "​": ""}


def fit_cp1252(text: str) -> str:
    """O CK2 lê Windows-1252: á ã ç é ê õ ô... cabem; o resto vira o equivalente ASCII."""
    out = []
    for ch in text:
        if ch in _SUBS:
            out.append(_SUBS[ch])
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            ascii_ = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(ascii_ or "?")
    return "".join(out)


# --------------------------------------------------------------------------- backends
_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
_RESPONSE_LIMIT = 2 * 1024 * 1024


def _secure_host(host: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(host)
        hostname = parsed.hostname
        parsed.port  # valida a porta antes de transferir dados
    except ValueError as error:
        raise BackendError("model.host inválido.") from error
    if (not hostname or parsed.scheme not in ("http", "https") or parsed.username is not None
            or "?" in host or "#" in host or any(c.isspace() or ord(c) < 32 for c in host)):
        raise BackendError("model.host deve ser uma URL HTTP(S) sem credenciais, consulta ou fragmento.")
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise BackendError("Use HTTPS em model.host para servidores fora desta máquina.")
    return host.rstrip("/")


def _retry_delay(error: urllib.error.HTTPError, default: float) -> float:
    value = error.headers.get("Retry-After", "").strip() if error.headers else ""
    if not value:
        return default
    try:
        if re.fullmatch(r"[0-9]{1,10}", value):
            delay = int(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return default
            delay = date.timestamp() - time.time()
    except (ValueError, OverflowError, OSError):
        return default
    if delay > 300:
        raise BackendError(f"HTTP {error.code}: servidor pediu {delay:.0f} s de espera. Retome depois.")
    return max(delay, default)


def _read_response(response, deadline: float) -> bytes:
    body = bytearray()
    while len(body) <= _RESPONSE_LIMIT:
        _check_cancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError("Tempo de leitura da resposta esgotado.")
        chunk = response.read1(min(64 * 1024, _RESPONSE_LIMIT + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
    raise TextBackendError("Resposta do serviço excedeu 2 MiB.")


def _post_json(url: str, payload: dict, timeout: float, headers: dict | None = None,
               retries: int = 2) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    for name, value in (headers or {}).items():
        req.add_unredirected_header(name, value)
    for attempt in range(retries + 1):
        _check_cancelled()
        delay = 1.5 * (attempt + 1)
        deadline = time.monotonic() + timeout
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                try:
                    result = json.loads(_read_response(resp, deadline).decode("utf-8"))
                except (UnicodeError, ValueError) as error:
                    raise TextBackendError("Resposta JSON inválida do modelo.") from error
                if not isinstance(result, dict):
                    raise TextBackendError("Resposta inesperada do modelo: esperado um objeto JSON.")
                return result
        except urllib.error.HTTPError as error:
            error.close()
            failure = f"HTTP {error.code} no servidor de tradução."
            if error.code in (400, 413, 422):
                raise TextBackendError(failure) from None
            if error.code not in _TRANSIENT_HTTP:
                raise BackendError(failure + " Confira o endereço, o modelo e as credenciais.") from None
            delay = _retry_delay(error, delay)
            if attempt == retries and (error.code == 429 or error.headers and error.headers.get("Retry-After")):
                _sleep(delay)
        except (OSError, http.client.HTTPException) as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise BackendError("Não foi possível verificar o certificado TLS do servidor.") from None
            failure = f"Falha de conexão com o servidor de tradução ({type(reason).__name__})."
        if attempt < retries:
            _sleep(delay)
    raise TextBackendError(failure)


class Backend:
    model_id = "base"
    uses_prompt = True      # False: o backend ignora prompt/glossário (ex.: Google)
    limiter: RateLimiter | None = None

    def chat(self, system: str, user: str) -> str:
        raise NotImplementedError


class OllamaBackend(Backend):
    def __init__(self, model: str, host: str = "http://localhost:11434",
                 temperature: float = 0.2, timeout: float = 180):
        self.model, self.host, self.temperature, self.timeout = model, _secure_host(host), temperature, timeout
        self.model_id = f"ollama:{model}"

    def chat(self, system: str, user: str) -> str:
        payload = {"model": self.model, "stream": False,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "options": {"temperature": self.temperature}}
        resp = _post_json(f"{self.host}/api/chat", payload, self.timeout)
        try:
            content = resp["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("conteúdo de tradução ausente ou inválido")
            return content
        except (KeyError, TypeError, ValueError) as e:
            raise TextBackendError("Resposta do Ollama sem conteúdo de tradução válido.") from e


class OpenAICompatBackend(Backend):
    """LM Studio, llama.cpp server, vLLM... qualquer servidor com /v1/chat/completions."""

    def __init__(self, model: str, host: str = "http://localhost:1234/v1",
                 temperature: float = 0.2, timeout: float = 180, api_key: str = ""):
        if api_key and any(ord(char) < 33 or ord(char) > 126 for char in api_key):
            raise BackendError("model.api_key deve conter um token sem espaços ou caracteres de controle.")
        self.model, self.host, self.temperature, self.timeout = model, _secure_host(host), temperature, timeout
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.model_id = f"openai:{model}"

    def chat(self, system: str, user: str) -> str:
        payload = {"model": self.model, "temperature": self.temperature,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        resp = _post_json(f"{self.host}/chat/completions", payload, self.timeout, self.headers)
        try:
            content = resp["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("conteúdo de tradução ausente ou inválido")
            return content
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise TextBackendError("Resposta do servidor sem conteúdo de tradução válido.") from e


def split_text(text: str, limit: int = 4000) -> list[str]:
    """Divide também textos sem pontuação, mantendo os marcadores inteiros."""
    if limit <= 0:
        raise ValueError("O limite de caracteres deve ser maior que zero.")
    if len(text) <= limit:
        return [text]
    blocks = []
    remaining = text.strip()
    while len(remaining) > limit:
        end = limit
        for token in re.finditer(r"XX\d+XX|\{\d+\}", remaining):
            if token.start() >= end:
                break
            if token.end() > end:
                if token.start() == 0:
                    raise TextBackendError("Um marcador protegido excede model.max_chars; aumente o limite.")
                end = token.start()
                break
        sentences = list(re.finditer(r"(?<=[.!?])\s+", remaining[:end + 1]))
        spaces = sentences or list(re.finditer(r"\s+", remaining[:end + 1]))
        if spaces and spaces[-1].start() > 0:
            end = spaces[-1].start()
        blocks.append(remaining[:end].rstrip())
        remaining = remaining[end:].lstrip()
    if remaining:
        blocks.append(remaining)
    return blocks


class GoogleBackend(Backend):
    """Google Tradutor via 'deep-translator' (pip install deep-translator). Os textos saem do PC.
    Pausas aleatórias entre pedidos e uma pausa longa a cada N, para o Google não bloquear."""
    uses_prompt = False
    model_id = "google"

    def __init__(self, source: str = "en", target: str = "pt", pause: tuple[float, float] = (1.0, 2.0),
                 long_every: int = 100, long_pause: float = 30, retry_wait: float = 20, translate_fn=None,
                 *, timeout: float = 30):
        if translate_fn is None:
            translate_fn = self._build_client(source, target, timeout)
        self.fn, self.pause, self.long_every = translate_fn, pause, long_every
        self.long_pause, self.retry_wait = long_pause, retry_wait
        self.count, self.lock = 0, threading.Lock()

    @staticmethod
    def _build_client(source: str, target: str, timeout: float):
        try:
            from bs4 import BeautifulSoup
            from deep_translator import GoogleTranslator
        except ImportError as error:
            raise BackendError("Falta a biblioteca: rode pip install deep-translator.") from error
        try:
            languages = GoogleTranslator(source=source, target=target)
        except Exception as error:
            raise BackendError("Idiomas inválidos para o Google Tradutor.") from error

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect())

        def translate(text: str) -> str:
            if languages.source == languages.target:
                return text
            params = urllib.parse.urlencode({"sl": languages.source, "tl": languages.target, "q": text})
            # O método translate() da dependência não aceita timeout de rede.
            deadline = time.monotonic() + timeout
            with opener.open("https://translate.google.com/m?" + params, timeout=timeout) as response:
                body = _read_response(response, deadline)
            soup = BeautifulSoup(body, "html.parser")
            element = soup.select_one("div.t0, div.result-container")
            if element is None:
                raise TextBackendError("O Google não retornou um resultado de tradução.")
            return element.get_text(strip=True)

        return translate

    def _call(self, text: str) -> str:
        for attempt in range(3):
            delay = self.retry_wait * (attempt + 1)
            # A chamada e a pausa pertencem ao mesmo pedido físico, incluindo
            # trechos de textos longos e retries de workers diferentes.
            with self.lock:
                _check_cancelled()
                try:
                    result = self.fn(text)
                    if isinstance(result, str) and result.strip():
                        return result
                    failure = "resposta vazia ou não textual"
                except urllib.error.HTTPError as error:
                    error.close()
                    failure = f"HTTP {error.code}"
                    delay = _retry_delay(error, delay)
                    if error.code == 429 or error.headers and error.headers.get("Retry-After"):
                        _sleep(delay)  # impede outros workers de ignorarem a pausa pedida pelo servidor
                        delay = 0
                except CancelledError:
                    raise
                except TextBackendError as error:
                    failure = str(error)
                except Exception as error:
                    reason = getattr(error, "reason", error)
                    if isinstance(reason, ssl.SSLCertVerificationError):
                        raise BackendError("Não foi possível verificar o certificado TLS do Google.") from None
                    failure = type(reason).__name__
                finally:
                    self.count += 1
                    _sleep(self.long_pause if self.long_every and self.count % self.long_every == 0
                           else random.uniform(*self.pause))
            if attempt < 2 and delay:
                print(f"\n  Erro do Google, esperando {delay:.0f}s...")
                _sleep(delay)
        raise TextBackendError(f"O Google não retornou uma tradução ({failure}).")

    def chat(self, system: str, user: str) -> str:
        m = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S)
        text = PH_RE.sub(lambda x: f"XX{x.group(1)}XX", m.group(1) if m else user)
        out = " ".join(self._call(p) for p in split_text(text))
        out = re.sub(r"XX\s*(\d+)\s*XX", r"{\1}", out, flags=re.I)
        return out


class RateLimiter:
    """Limita pedidos por minuto e por hora (janela deslizante) + pausa aleatória entre pedidos,
    pausa longa a cada N pedidos. Imita um humano, evitando bloqueio por IP."""

    def __init__(self, per_minute: int = 20, per_hour: int = 600, pause: tuple[float, float] = (1.5, 4.0),
                 long_every: int = 80, long_pause: tuple[float, float] = (40, 90), sleep=_sleep, now=time.monotonic):
        self.per_minute, self.per_hour, self.pause = per_minute, per_hour, pause
        self.long_every, self.long_pause = long_every, long_pause
        self.sleep, self.now = sleep, now
        self.stamps: list[float] = []
        self.count = 0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            _check_cancelled()
            t = self.now()
            self.stamps = [x for x in self.stamps if t - x < 3600]
            last_min = [x for x in self.stamps if t - x < 60]
            wait = 0.0
            if self.per_minute and len(last_min) >= self.per_minute:
                wait = 60 - (t - last_min[-self.per_minute]) + random.uniform(0.5, 2)
            if self.per_hour and len(self.stamps) >= self.per_hour:
                hourly = 3600 - (t - self.stamps[-self.per_hour]) + random.uniform(5, 30)
                print(f"\n  Limite por hora atingido, esperando {hourly / 60:.0f} min (progresso salvo)...")
                wait = max(wait, hourly)
            if wait:
                self.sleep(wait)
            self.count += 1
            if self.count > 1:
                if self.long_every and self.count % self.long_every == 0:
                    self.sleep(random.uniform(*self.long_pause))
                else:
                    self.sleep(random.uniform(*self.pause))
            self.stamps.append(self.now())


class TranslatorsBackend(Backend):
    """Tradução via biblioteca 'translators' (pip install translators). Usa vários serviços
    (google, bing, yandex, alibaba...) em rodízio: se um bloquear, ele fica de "castigo" (cooldown)
    e o próximo assume. Backoff exponencial com jitter, limite por minuto/hora e pausas aleatórias."""
    uses_prompt = False
    model_id = "translators"

    def __init__(self, providers: list[str] | None = None, source: str = "en", target: str = "pt",
                 limiter: RateLimiter | None = None, max_retries: int = 4, backoff_base: float = 15,
                 cooldown: float = 600, max_chars: int = 3000, translate_fn=None, sleep=_sleep,
                 *, timeout: float = 30):
        if translate_fn is None:
            # A biblioteca consulta serviços de geolocalização sem timeout
            # durante o import quando a região não é informada.
            region_key = "translators_default_region"
            configured_region = os.environ.get(region_key)
            os.environ[region_key] = configured_region or "EN"
            try:
                import translators as ts
            except ImportError as e:
                raise BackendError("Falta a biblioteca: rode  pip install translators") from e
            except Exception as error:
                raise BackendError(f"Falha ao iniciar a biblioteca translators ({type(error).__name__}).") from error
            finally:
                if configured_region is None:
                    os.environ.pop(region_key, None)
                else:
                    os.environ[region_key] = configured_region
            def translate_fn(text, provider, src, dst):
                return ts.translate_text(text, translator=provider, from_language=src, to_language=dst,
                                         timeout=timeout)
        self.fn, self.src, self.dst = translate_fn, source, target
        self.providers = list(providers or ["google", "bing", "alibaba", "yandex"])
        self.limiter = limiter or RateLimiter()
        self.max_retries, self.backoff_base, self.cooldown = max_retries, backoff_base, cooldown
        self.max_chars, self.sleep = max_chars, sleep
        self.blocked_until: dict[str, float] = {}
        self.fails: dict[str, int] = {}
        self.idx = 0
        self.lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.stats: dict[str, int] = {}

    def _pick(self) -> str | None:
        now = time.monotonic()
        with self.lock:
            for i in range(len(self.providers)):
                p = self.providers[(self.idx + i) % len(self.providers)]
                if self.blocked_until.get(p, 0) <= now:
                    self.idx = (self.idx + i) % len(self.providers)
                    return p
        return None

    def _punish(self, p: str, err: Exception) -> None:
        with self.lock:
            self.fails[p] = self.fails.get(p, 0) + 1
            msg = str(err).lower()
            hard = any(k in msg for k in ("429", "too many", "blocked", "captcha", "403", "rate"))
            if hard or self.fails[p] >= 2:
                self.blocked_until[p] = time.monotonic() + self.cooldown * (1 + random.random() * 0.3)
                self.fails[p] = 0
                self.idx += 1
                print(f"\n  '{p}' bloqueou ou falhou. Em pausa por {self.cooldown / 60:.0f} min, trocando de serviço.")

    def _call(self, text: str) -> str:
        # A biblioteca usa sessões compartilhadas por provedor.
        with self.request_lock:
            return self._call_locked(text)

    def _call_locked(self, text: str) -> str:
        last: Exception | None = None
        attempts = self.max_retries * len(self.providers)
        for attempt in range(attempts):
            _check_cancelled()
            p = self._pick()
            if p is None:
                if last is not None or attempt == attempts - 1:
                    break
                with self.lock:
                    soonest = min(self.blocked_until.get(provider, 0) for provider in self.providers) - time.monotonic()
                print(f"\n  Todos os serviços em pausa; esperando {max(soonest, 0) / 60:.1f} min...")
                self.sleep(max(soonest, 1))
                continue
            self.limiter.wait()
            try:
                r = self.fn(text, p, self.src, self.dst)
                if isinstance(r, str) and r.strip():
                    with self.lock:
                        self.fails[p] = 0
                        self.stats[p] = self.stats.get(p, 0) + 1
                    return r
                raise BackendError("resposta vazia")
            except CancelledError:
                raise
            except Exception as e:
                last = e
                self._punish(p, e)
                w = min(self.backoff_base * (2 ** min(attempt, 4)), 300) * random.uniform(0.7, 1.3)
                if attempt < attempts - 1:
                    self.sleep(w)
        reason = type(last).__name__ if last is not None else "serviços em pausa"
        raise TextBackendError(f"Nenhum serviço retornou tradução ({reason}).")

    # Variações que os serviços produzem a partir de "XX1XX": espaços, minúsculas, X cirílico/grego, "XX 1XX"...
    _UNMARK_RE = re.compile(r"[XxХхΧχ]\s*[XxХхΧχ]\s*(\d+)\s*[XxХхΧχ]\s*[XxХхΧχ]")

    @classmethod
    def _unmark(cls, out: str) -> str:
        return cls._UNMARK_RE.sub(r"{\1}", out)

    @staticmethod
    def _ok(out: str, text: str) -> bool:
        """Mesmos marcadores (nem perdeu, nem repetiu, nem inventou), sem XX sobrando e sem chaves soltas."""
        if sorted(PH_RE.findall(out)) != sorted(PH_RE.findall(text)):
            return False
        rest = PH_RE.sub("", out)
        return "{" not in rest and "}" not in rest and not re.search(r"[XxХх]{2}\s*\d", rest)

    def _whole(self, text: str) -> str:
        marked = PH_RE.sub(lambda x: f"XX{x.group(1)}XX", text)
        return self._unmark(" ".join(self._call(part) for part in split_text(marked, self.max_chars)))

    def _segmentwise(self, text: str) -> str:
        """Plano C: traduz só os trechos entre os códigos; os códigos nunca passam pelo serviço."""
        out = []
        for part in re.split(r"(\{\d+\})", text):
            if not part or PH_RE.fullmatch(part) or not any(c.isalpha() for c in part):
                out.append(part)
                continue
            lead, trail = part[: len(part) - len(part.lstrip())], part[len(part.rstrip()):]
            translated = " ".join(self._call(chunk).strip() for chunk in split_text(part.strip(), self.max_chars))
            out.append(lead + translated + trail)
        return "".join(out)

    def chat(self, system: str, user: str) -> str:
        m = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S)
        text = m.group(1) if m else user
        if not PH_RE.search(text):
            return self._whole(text)
        # Plano A: frase inteira (melhor contexto). Plano B: outro serviço. Plano C: trecho a trecho (100% seguro).
        for _ in range(min(2, len(self.providers))):
            out = self._whole(text)
            if self._ok(out, text):
                return out
            with self.lock:
                self.stats["marcadores_rejeitados"] = self.stats.get("marcadores_rejeitados", 0) + 1
                self.idx += 1
        with self.lock:
            self.stats["trecho_a_trecho"] = self.stats.get("trecho_a_trecho", 0) + 1
        return self._segmentwise(text)


def install_argos_package(source: str = "en", target: str = "pt") -> None:
    """Baixa e instala o pacote de idiomas do Argos (precisa de internet só uma vez)."""
    try:
        import argostranslate.package as pkg
    except ImportError as e:
        raise BackendError("Falta a biblioteca: rode  pip install argostranslate") from e
    except Exception as error:
        raise BackendError(f"Falha ao carregar o instalador do Argos: {error}") from error
    try:
        pkg.update_package_index()
        found = next((p for p in pkg.get_available_packages()
                      if p.from_code == source and p.to_code == target), None)
        if found is not None:
            pkg.install_from_path(found.download())
            return
    except Exception as error:
        raise BackendError(f"Falha ao instalar o pacote do Argos: {error}") from error
    raise BackendError(f"O Argos não tem pacote {source}->{target}.")


class ArgosBackend(Backend):
    """Tradução automática OFFLINE (Argos Translate): sem conta, sem limite, sem enviar texto a ninguém.
    Instalação:  pip install argostranslate   e depois   python ck2tr.py argos-setup"""
    uses_prompt = False
    model_id = "argos"

    def __init__(self, source: str = "en", target: str = "pt", translate_fn=None):
        if translate_fn is None:
            try:
                import argostranslate.translate as tr
                langs = {language.code: language for language in tr.get_installed_languages()}
                translation = langs[source].get_translation(langs[target]) if source in langs and target in langs else None
            except ImportError as e:
                raise BackendError("Falta a biblioteca: rode  pip install argostranslate") from e
            except Exception as error:
                raise BackendError(f"Falha ao carregar os recursos do Argos: {error}") from error
            if translation is None:
                raise BackendError(f"O Argos não encontrou tradução {source}->{target}. "
                                   "Para en->pt, rode python ck2tr.py argos-setup.")
            translate_fn = translation.translate
        self.fn = translate_fn
        self.lock = threading.Lock()

    def _tr(self, text: str) -> str:
        output = []
        for part in split_text(text, 2000):
            try:
                with self.lock:
                    _check_cancelled()
                    translated = self.fn(part)
            except CancelledError:
                raise
            except (FileNotFoundError, PermissionError) as error:
                raise BackendError(f"Falha ao acessar os recursos do Argos: {error}") from error
            except Exception as error:
                raise TextBackendError(f"Falha do Argos ao traduzir um trecho: {error}") from error
            if not isinstance(translated, str) or not translated.strip():
                raise TextBackendError("O Argos devolveu uma resposta vazia ou não textual.")
            output.append(translated)
        return " ".join(output)

    def _segmentwise(self, text: str) -> str:
        """Plano B: traduz só os trechos entre os marcadores {n}; os marcadores nunca passam pelo tradutor."""
        out = []
        for part in re.split(r"(\{\d+\})", text):
            if not part or PH_RE.fullmatch(part) or not any(c.isalpha() for c in part):
                out.append(part)
                continue
            lead, trail = part[: len(part) - len(part.lstrip())], part[len(part.rstrip()):]
            out.append(lead + self._tr(part.strip()) + trail)
        return "".join(out)

    def chat(self, system: str, user: str) -> str:
        m = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S)
        text = m.group(1) if m else user
        marked = PH_RE.sub(lambda x: f"XX{x.group(1)}XX", text)
        out = TranslatorsBackend._unmark(self._tr(marked))
        if not TranslatorsBackend._ok(out, text):
            out = self._segmentwise(text)
        return out


class EchoBackend(Backend):
    """Sem IA: devolve o texto com 'PT> ' na frente. Serve para testar todo o pipeline."""
    model_id = "echo"

    def chat(self, system: str, user: str) -> str:
        m = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S)
        return "PT> " + (m.group(1) if m else user)


# --------------------------------------------------------------------------- cache
class Cache:
    def __init__(self, path: str = ":memory:", *, read_only: bool = False):
        self.read_only = read_only
        if path != ":memory:":
            file = Path(path).expanduser().resolve()
            if read_only:
                path = file.as_uri() + "?mode=ro" if file.exists() else ":memory:"
            else:
                file.parent.mkdir(parents=True, exist_ok=True)
                path = str(file)
        self.db = sqlite3.connect(path, check_same_thread=False, uri=read_only,
                                  isolation_level=None, timeout=30)
        self.lock = threading.Lock()
        # A mesma frase pode chegar de arquivos/workers diferentes ao mesmo tempo.
        # O lock da consulta SQL não basta para evitar duas traduções concorrentes.
        self.work_locks = [threading.Lock() for _ in range(64)]
        self.closed = False
        if not read_only or path == ":memory:":
            self.db.execute("CREATE TABLE IF NOT EXISTS cache "
                            "(k TEXT PRIMARY KEY, src TEXT, dst TEXT, model TEXT, ts REAL)")

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, k: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT dst FROM cache WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def models(self) -> list[str]:
        """Backends que já têm traduções no cache, do que tem mais entradas para o que tem menos."""
        with self.lock:
            rows = self.db.execute("SELECT model, COUNT(*) FROM cache GROUP BY model ORDER BY 2 DESC").fetchall()
        return [m for m, _ in rows if m and m != "echo"]

    def put(self, k: str, src: str, dst: str, model: str) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("O cache foi aberto apenas para leitura.")
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                            (k, src, dst, model, time.time()))

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.db.close()
                self.closed = True


# --------------------------------------------------------------------------- tradutor
@dataclass
class Result:
    status: str          # ok | cache | skip | falha
    text: str            # texto final; em skip/falha é o original
    note: str = ""


def clean_output(raw: str, src: str) -> str:
    s = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I).strip()
    s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
    s = re.sub(r"\n?```$", "", s).strip()
    s = re.sub(r"</?texto>", "", s).strip()
    s = re.sub(r"^(tradução|traducao|translation|português|portugues)\s*:\s*", "", s, flags=re.I)
    if len(s) >= 2 and s[0] == s[-1] == '"' and not (src.startswith('"') and src.endswith('"')):
        s = s[1:-1].strip()
    return re.sub(r"\s*\n\s*", " ", s)      # quebra de linha real quebraria o CSV


class Translator:
    def __init__(self, backend: Backend, cache: Cache, glossary: dict[str, str] | None = None,
                 keep: list[str] | None = None, context: str = "", max_attempts: int = 3,
                 reuse_from: list[str] | str | None = None, paranoid: bool = False, post=None,
                 game: str = "ck2"):
        self.backend, self.cache = backend, cache
        self.profile = get_profile(game)
        self.token_re = EU4_TOKEN_RE if self.profile.id == "eu4" else TOKEN_RE
        self.post = post                # função opcional de pós-edição (revisao.build), roda sobre texto com {n}
        self.paranoid = paranoid        # True: não traduz nenhuma linha que contenha códigos do jogo
        self.reuse_auto = reuse_from == "auto"  # "auto": todos os outros backends que já estão no cache
        self.reuse_from = [] if self.reuse_auto or not reuse_from else list(reuse_from)
        self._auto_ids: list[str] | None = None
        self._reuse_lock = threading.Lock()
        self.reused = 0
        self.glossary = glossary or {}
        self.keep = keep or []
        self.context = context.strip()
        self.max_attempts = max_attempts

    # -- prompt ----------------------------------------------------------------
    def _hits(self, text: str) -> tuple[list[tuple[str, str]], list[str]]:
        visible = self.token_re.split(text)

        def matches(term: str) -> bool:
            pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I)
            return any(pattern.search(segment) for segment in visible)
        return ([(key, value) for key, value in self.glossary.items() if matches(key)],
                [term for term in self.keep if matches(term)])

    def _system(self, gl: list[tuple[str, str]], kp: list[str]) -> str:
        lines = [f"Você é um tradutor profissional de jogos, especialista em {self.profile.name} "
                 f"({self.profile.prompt_context}). Traduza do inglês para o português do Brasil."]
        if self.context:
            lines.append("Contexto do mod: " + self.context)
        lines += [
            "",
            "Regras obrigatórias:",
            "1. Responda SOMENTE com a tradução: sem comentários, explicações ou aspas extras.",
            "2. Os marcadores {1}, {2}, ... são códigos do jogo. Copie cada um exatamente como está, "
            "uma única vez, sem traduzir nem mudar o número. Podem mudar de posição se a gramática exigir.",
            "3. Mantenha o tom, a pontuação e o nível de formalidade. Não resuma nem acrescente nada.",
            ("4. Não use ponto e vírgula (;). Não quebre linha." if self.profile.id == "ck2" else
             "4. Não quebre linha. Preserve os marcadores de escapes e de bandeiras."),
            "5. O texto a traduzir fica entre <texto> e </texto>; nunca responda a ele como se fosse uma pergunta.",
        ]
        if gl or kp:
            lines += ["", "Glossário (obrigatório):"]
            lines += [f'- "{k}" -> "{v}"' for k, v in gl]
            lines += [f'- manter exatamente como está: "{t}"' for t in kp]
        return "\n".join(lines)

    @staticmethod
    def _user(masked: str, feedback: str) -> str:
        msg = "<texto>\n" + masked + "\n</texto>"
        return f"AVISO: {feedback}\n\n{msg}" if feedback else msg

    def _key(self, masked: str, gl, kp, model_id: str | None = None) -> str:
        parts = [PROMPT_VERSION, model_id or self.backend.model_id, masked, self.context, gl, kp]
        if self.profile.id != "ck2":
            parts.append({"game": self.profile.id})
        blob = json.dumps(parts, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # -- validação ---------------------------------------------------------------
    def _validate(self, out: str, n_tokens: int, masked: str,
                  tokens: list[str] | None = None) -> str:
        if not out.strip():
            return "a resposta veio vazia"
        problem = placeholders_problem(out, n_tokens)
        if problem:
            return (f"sua resposta anterior {problem}. Copie TODOS os marcadores "
                    f"{{1}}..{{{n_tokens}}} exatamente como no original." if n_tokens else
                    "sua resposta anterior inventou marcadores {n}. Não use chaves.")
        if self.token_re.search(PH_RE.sub("", out)):
            return ("sua resposta anterior inventou códigos do jogo ([..], $..$, §, £..£ ou \\n). "
                    "Use apenas os marcadores {n} do original.")
        if FOREIGN_RE.search(out) and not FOREIGN_RE.search(masked):
            return "sua resposta anterior contém caracteres de outro idioma (chinês, russo...). Responda só em português do Brasil."
        if any(PH_RE.sub("", out).count(char) != PH_RE.sub("", masked).count(char)
               for char in "$§£"):
            return "sua resposta anterior inventou ou alterou delimitadores de códigos do jogo"
        if len(out) > 4 * len(masked) + 40:
            return "a resposta ficou longa demais; devolva só a tradução"
        for quote in ('"', "'"):
            def count_quotes(text: str) -> int:
                return (len(re.findall(r"(?<!\w)'|'(?!\w)", text)) if quote == "'"
                        else text.count(quote))
            if count_quotes(out) % 2 and not count_quotes(masked) % 2:
                return "a tradução introduziu aspas desparelhadas"
        if tokens is not None:
            problem = color_markers_problem(masked, out, tokens)
            if problem:
                return problem
        problems = quality_problems(masked, out)
        if problems:
            return "; ".join(problems)
        if unchanged_prose(masked, out, keep=self.keep, glossary=self.glossary):
            return "o texto em inglês foi devolvido sem tradução"
        return ""

    def reuse_ids(self) -> list[str]:
        if self.reuse_auto:
            if self._auto_ids is None:
                with self._reuse_lock:
                    if self._auto_ids is None:
                        self._auto_ids = [m for m in self.cache.models() if m != self.backend.model_id]
            return self._auto_ids
        return [m for m in self.reuse_from if m != self.backend.model_id]

    def _reuse(self, core: str, gl, kp) -> str | None:
        """Procura a tradução no cache de outros backends (reuse_cache_from)."""
        for mid in self.reuse_ids():
            m2, t2 = mask(core, *((kp, gl) if mid in NON_PROMPT_IDS else (None, None)),
                          token_re=self.token_re)
            hit = self.cache.get(self._key(m2, gl, kp, mid))
            if hit is None or self._validate(hit, len(t2), m2, t2):   # entrada antiga com defeito: ignora
                continue
            return self._render(m2, hit, t2)
        return None

    def _post(self, masked_out: str, source: str, tokens: list[str]) -> str:
        if self.post is None:
            return masked_out
        edited = self.post(masked_out)
        # O cache guarda a resposta validada antes da revisão. Uma regra de
        # usuário inválida não pode apagar códigos nem inventar índices que
        # fariam unmask falhar; nesse caso conserva-se a tradução já segura.
        if self._validate(edited, len(tokens), source, tokens):
            return masked_out
        return edited

    def _render(self, source: str, output: str, tokens: list[str]) -> str:
        output = normalize_punct(source, self._punctuation(output))
        output = self._post(output, source, tokens)
        return unmask(restore_marker_spaces(source, output), tokens)

    def _punctuation(self, text: str) -> str:
        return text.replace(";", ",") if self.profile.id == "ck2" else text

    # -- API ---------------------------------------------------------------------
    def translate(self, src: str, dry: bool = False) -> Result:
        """dry=True: não chama o backend; devolve status "pendente" para o que ainda precisaria ser traduzido."""
        core = src.strip()
        if not core:
            return Result("skip", src, "vazio")
        lead, trail = src[: len(src) - len(src.lstrip())], src[len(src.rstrip()):]
        if self.paranoid and (any(c in core for c in STRUCT_CHARS)
                              or self.token_re.search(core)):
            return Result("skip", src, "tem códigos do jogo (modo paranoid)")
        gl, kp = self._hits(core)
        # Séries geradas de tecnologia: traduz o título uma vez e mantém o nível
        # fora do modelo. Não se aplica a descrições nem a frases terminadas em número.
        technology = re.fullmatch(r"((?:Administrative|Diplomatic|Military) Technology) ([IVXLCDM]+|[0-9]+)", core)
        base_hits = self._hits(technology.group(1)) if technology else ([], [])
        if technology and (gl, kp) == base_hits:
            title = self.translate(technology.group(1), dry=dry)
            if title.status in ("ok", "cache"):
                return Result(title.status, lead + title.text + " " + technology.group(2) + trail,
                              "padrão de tecnologia numerada")
            return Result(title.status, src, title.note)
        # Backend sem prompt (translators, Google, Argos): "manter" e glossário viram marcadores, como os
        # códigos do jogo (o glossário entra com a tradução já pronta). Backend com prompt (Ollama): vão no prompt.
        masked, tokens = mask(core, *((None, None) if self.backend.uses_prompt else (kp, gl)),
                              token_re=self.token_re)
        if not has_letters(masked):
            if gl and not self.backend.uses_prompt and unmask(masked, tokens) != core:
                return Result("ok", lead + unmask(masked, tokens) + trail, "só glossário")
            return Result("skip", src, "sem texto traduzível")
        key = self._key(masked, gl, kp)
        with self.cache.work_locks[int(key[:8], 16) % len(self.cache.work_locks)]:
            return self._translate_masked(core, lead, trail, masked, tokens, gl, kp, key, dry)

    def _translate_masked(self, core, lead, trail, masked, tokens, gl, kp, key, dry) -> Result:
        cached = self.cache.get(key)
        if cached is not None and not self._validate(cached, len(tokens), masked, tokens):
            return Result("cache", lead + self._render(masked, cached, tokens) + trail)
        other = self._reuse(core, gl, kp)
        if other is not None:
            self.reused += 1
            return Result("cache", lead + other + trail)

        if dry:
            return Result("pendente", lead + core + trail)
        system, feedback, problem = self._system(gl, kp), "", ""
        attempts = self.max_attempts if self.backend.uses_prompt else 2
        for _ in range(attempts):
            _check_cancelled()
            out = clean_output(self.backend.chat(system, self._user(masked, feedback)), core)
            out = normalize_punct(masked, out)
            problem = self._validate(out, len(tokens), masked, tokens)
            if not problem:
                out = self._punctuation(out)
                self.cache.put(key, core, out, self.backend.model_id)
                return Result("ok", lead + self._render(masked, out, tokens) + trail)
            feedback = problem
        return Result("falha", lead + core + trail, problem)
