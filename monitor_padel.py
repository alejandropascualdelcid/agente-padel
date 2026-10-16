#!/usr/bin/env python3
"""Monitor de pistas de pádel libres en Coslada Dreamfit (Playtomic) con avisos por Telegram.

Uso:
    python monitor_padel.py --once            # una pasada (cron / GitHub Actions)
    python monitor_padel.py --loop            # daemon, cada INTERVAL_SECONDS (60s por defecto)
    python monitor_padel.py --once --dry-run  # imprime el mensaje en vez de enviarlo

Configuración por variables de entorno (o fichero .env en el mismo directorio):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (obligatorias salvo en --dry-run)
    DAYS_AHEAD=12, START_MIN=18:30, START_MAX=21:00, SLOT_DURATION=90,
    INTERVAL_SECONDS=60, STATE_FILE=state.json, API_TIME_ZONE=UTC,
    COURT_NAMES='{"<resource_id>": "Padel 1", ...}'  (opcional, fuerza nombres)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# --------------------------------------------------------------------------- #
# Constantes del club
# --------------------------------------------------------------------------- #
TENANT_ID = "a328dd8a-031b-4230-adc5-928a53d16ad6"
SPORT_ID = "PADEL"
CLUB_SLUG = "coslada-dreamfit"
CLUB_NAME = "Coslada Dreamfit"
CLUB_URL = f"https://playtomic.com/clubs/{CLUB_SLUG}"
AVAILABILITY_URL = "https://playtomic.com/api/clubs/availability"
TZ = ZoneInfo("Europe/Madrid")
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=8.0)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": CLUB_URL,
}

WEEKDAYS_ES = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
TELEGRAM_MAX_LEN = 4000  # límite real 4096; dejamos margen

log = logging.getLogger("monitor_padel")


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path) -> None:
    """Carga un .env sencillo (KEY=VALUE) sin pisar variables ya definidas."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: str
    days_ahead: int
    start_min: str
    start_max: str
    duration: int
    interval: int
    state_file: Path
    court_names: dict[str, str]
    api_tz: ZoneInfo = ZoneInfo("UTC")

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            days_ahead=int(os.getenv("DAYS_AHEAD", "12")),
            start_min=os.getenv("START_MIN", "18:30"),
            start_max=os.getenv("START_MAX", "21:00"),
            duration=int(os.getenv("SLOT_DURATION", "90")),
            interval=int(os.getenv("INTERVAL_SECONDS", "60")),
            state_file=Path(os.getenv("STATE_FILE", "state.json")),
            court_names=json.loads(os.getenv("COURT_NAMES", "") or "{}"),
            api_tz=ZoneInfo(os.getenv("API_TIME_ZONE", "UTC")),
        )


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, order=True)
class Slot:
    day: date
    start: str  # "HH:MM", hora local de Madrid tal cual la devuelve la API
    court: str
    resource_id: str
    duration: int
    price: str

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()}|{self.start}|{self.resource_id}|{self.duration}"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def get_json(client: httpx.Client, url: str, params: dict | None = None, retries: int = 2):
    """GET con reintentos para 429/5xx/errores de red. Lanza la última excepción."""
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, params=params)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 403:
                log.warning("403 en %s: bloqueo de Cloudflare (IP de datacenter?)", url)
                raise
            if attempt >= retries:
                raise
            time.sleep(2 ** attempt)


# --------------------------------------------------------------------------- #
# Nombres de pistas
# --------------------------------------------------------------------------- #
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RESOURCE_OBJ = re.compile(r'\{[^{}]*?"resource_?[iI]d"\s*:\s*"(' + _UUID + r')"[^{}]*\}')
_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')
_SPORT = re.compile(r'"sport(?:_id)?"\s*:\s*"([^"]+)"')


def parse_court_names(html: str) -> dict[str, str]:
    """Extrae resourceId -> name del payload embebido en la web del club.

    La página lleva los recursos como JSON escapado dentro de un <script>
    ({\\"resourceId\\":\\"...\\",\\"name\\":\\"Padel 7\\",...}), así que se desescapa
    y se buscan los objetos planos que contienen resourceId + name.
    """
    text = html.replace('\\"', '"')
    names: dict[str, str] = {}
    for m in _RESOURCE_OBJ.finditer(text):
        obj = m.group(0)
        name = _NAME.search(obj)
        sport = _SPORT.search(obj)
        if not name or (sport and sport.group(1) != SPORT_ID):
            continue
        names.setdefault(m.group(1), name.group(1).strip())
    return names


def fetch_court_names(client: httpx.Client) -> dict[str, str]:
    """Resuelve resource_id -> nombre real ("Padel 1"...) desde la web del club."""
    try:
        resp = client.get(CLUB_URL)
        resp.raise_for_status()
        names = parse_court_names(resp.text)
        if names:
            log.info("Nombres de pistas resueltos desde la web del club (%d)", len(names))
        else:
            log.warning("La web del club no contiene nombres de pistas reconocibles")
        return names
    except httpx.HTTPError as exc:
        log.warning("No se pudieron obtener nombres desde %s: %s", CLUB_URL, exc)
        return {}


def court_label(resource_id: str, names: dict[str, str]) -> str:
    return names.get(resource_id) or f"Pista {resource_id[:8]}"


# --------------------------------------------------------------------------- #
# Disponibilidad
# --------------------------------------------------------------------------- #
def target_days(today: date, days_ahead: int) -> list[date]:
    """Días laborables (L-V) desde hoy hasta hoy + days_ahead (incluidos)."""
    days = (today + timedelta(days=i) for i in range(days_ahead + 1))
    return [d for d in days if d.weekday() < 5]


def fetch_availability(client: httpx.Client, day: date) -> list[dict]:
    data = get_json(
        client,
        AVAILABILITY_URL,
        params={"tenant_id": TENANT_ID, "date": day.isoformat(), "sport_id": SPORT_ID},
    )
    if not isinstance(data, list):
        raise ValueError(f"Respuesta inesperada de availability: {type(data).__name__}")
    return data


def slot_local_start(start_date: str, start_time: str, api_tz: ZoneInfo) -> datetime:
    """Convierte la fecha/hora del slot (en la zona de la API, UTC) a hora de Madrid.

    Comprobado con datos reales: el mismo turno de las 21:30 aparece como 19:30
    antes del cambio de hora de octubre y como 20:30 después, así que la API
    devuelve UTC. Se convierte con zoneinfo (respeta el horario de verano),
    nunca con un desfase fijo.
    """
    naive = datetime.fromisoformat(f"{start_date[:10]}T{start_time[:8]}")
    return naive.replace(tzinfo=api_tz).astimezone(TZ)


def filter_slots(
    raw: list[dict], day: date, cfg: Config, names: dict[str, str], now: datetime
) -> list[Slot]:
    """Aplica el filtro estricto sobre la hora de inicio YA convertida a hora de Madrid."""
    slots: list[Slot] = []
    for resource in raw:
        rid = resource.get("resource_id", "")
        start_date = resource.get("start_date") or day.isoformat()
        for s in resource.get("slots") or []:
            if s.get("duration") != cfg.duration or not s.get("start_time"):
                continue
            local = slot_local_start(start_date, str(s["start_time"]), cfg.api_tz)
            start = local.strftime("%H:%M")
            # Nos quedamos solo con los turnos que en Madrid caen en el día pedido.
            if local.date() != day:
                continue
            if not (cfg.start_min <= start <= cfg.start_max):
                continue
            if local <= now:
                continue
            slots.append(
                Slot(
                    day=day,
                    start=start,
                    court=court_label(rid, names),
                    resource_id=rid,
                    duration=s["duration"],
                    price=str(s.get("price") or "").strip(),
                )
            )
    return sorted(slots, key=slot_sort_key)


def slot_sort_key(slot: Slot):
    """Día, hora y número de pista como número (Padel 3 antes que Padel 11)."""
    digits = "".join(ch for ch in slot.court if ch.isdigit())
    return (slot.day, slot.start, int(digits) if digits else 999, slot.court)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def md_escape(text: str) -> str:
    """Escapa caracteres especiales del Markdown 'legacy' de Telegram."""
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def day_label(d: date) -> str:
    return f"{WEEKDAYS_ES[d.weekday()]} {d:%d/%m}"


def format_messages(slots: list[Slot], cfg: Config) -> list[str]:
    """Genera uno o varios mensajes (troceados por día si exceden el límite)."""
    header = (
        f"🎾 *¡Pistas libres en {CLUB_NAME}!*\n"
        f"🕡 Inicio {cfg.start_min}–{cfg.start_max} · {cfg.duration} min\n"
    )
    blocks: list[str] = []
    by_day: dict[date, list[Slot]] = {}
    for s in slots:
        by_day.setdefault(s.day, []).append(s)
    for d, day_slots in sorted(by_day.items()):
        lines = [f"📅 *{day_label(d)}*"]
        for s in day_slots:
            price = f" ({md_escape(s.price)})" if s.price else ""
            lines.append(f"• {s.start} ➜ {md_escape(s.court)}{price}")
        lines.append(f"👉 [Reservar {day_label(d)}]({CLUB_URL}?date={d.isoformat()})")
        blocks.append("\n".join(lines))

    messages: list[str] = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > TELEGRAM_MAX_LEN and current != header:
            messages.append(current.rstrip())
            current = header
        current += "\n" + block + "\n"
    if blocks:
        messages.append(current.rstrip())
    return messages


def send_telegram(client: httpx.Client, cfg: Config, text: str) -> None:
    resp = client.post(
        f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage",
        json={
            "chat_id": cfg.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    if resp.status_code != 200 or not resp.json().get("ok"):
        # No logueamos la URL: contiene el token.
        raise RuntimeError(f"Telegram respondió {resp.status_code}: {resp.text[:300]}")


# --------------------------------------------------------------------------- #
# Estado anti-spam
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Estado ilegible (%s), se empieza de cero", exc)
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Ciclo principal
# --------------------------------------------------------------------------- #
def run_once(cfg: Config, client: httpx.Client, dry_run: bool = False, now: datetime | None = None) -> int:
    """Una pasada completa. Devuelve 0 si OK, 1 si no se pudo consultar ningún día.

    Anti-spam: el estado guarda los slots libres vistos en la pasada anterior. Solo
    se avisa de los que aparecen nuevos; si una pista se reserva y luego se libera
    de nuevo, vuelve a notificarse (es una oportunidad nueva).
    """
    now = now or datetime.now(TZ)
    today = now.date()
    state = load_state(cfg.state_file)
    previous: set[str] = set(state.get("available", []))

    names = dict(state.get("court_names", {}))
    fresh = fetch_court_names(client)
    names.update(fresh)
    names.update(cfg.court_names)

    days = target_days(today, cfg.days_ahead)
    current: dict[str, Slot] = {}
    failed_days: set[str] = set()
    for d in days:
        try:
            raw = fetch_availability(client, d)
        except (httpx.HTTPError, ValueError) as exc:
            log.error("Fallo consultando %s: %s", d, exc)
            failed_days.add(d.isoformat())
            continue
        for s in filter_slots(raw, d, cfg, names, now):
            current[s.key] = s
        log.info("%s: %d slots en franja", day_label(d), sum(1 for s in current.values() if s.day == d))

    new_slots = sorted((s for k, s in current.items() if k not in previous), key=slot_sort_key)
    sent_ok = True
    if new_slots:
        log.info("%d slots nuevos -> notificando", len(new_slots))
        for msg in format_messages(new_slots, cfg):
            if dry_run:
                print(msg, "\n" + "-" * 40)
                continue
            try:
                send_telegram(client, cfg, msg)
            except (httpx.HTTPError, RuntimeError) as exc:
                log.error("Error enviando a Telegram: %s", exc)
                sent_ok = False
    else:
        log.info("Sin novedades")

    # Si Telegram falla, no marcamos los nuevos como vistos (se reintenta la próxima vez).
    seen = set(current) if sent_ok else set(current) - {s.key for s in new_slots}
    # Días que no se pudieron consultar: conservamos lo anterior para no re-notificar.
    seen |= {k for k in previous if k.split("|", 1)[0] in failed_days}
    # Purga de días pasados.
    seen = {k for k in seen if k.split("|", 1)[0] >= today.isoformat()}

    if not dry_run:
        save_state(
            cfg.state_file,
            {"available": sorted(seen), "court_names": names, "updated_at": now.isoformat()},
        )
    return 1 if days and len(failed_days) == len(days) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="una sola pasada (por defecto)")
    mode.add_argument("--loop", action="store_true", help="bucle infinito")
    parser.add_argument("--interval", type=int, help="segundos entre pasadas en --loop")
    parser.add_argument("--dry-run", action="store_true", help="imprime en vez de enviar y no guarda estado")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # evita loguear la URL con el token

    load_dotenv(Path(__file__).with_name(".env"))
    cfg = Config.from_env()
    if not args.dry_run and not (cfg.bot_token and cfg.chat_id):
        log.error("Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID")
        return 2

    with httpx.Client(headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        if not args.loop:
            return run_once(cfg, client, dry_run=args.dry_run)
        interval = args.interval or cfg.interval
        log.info("Modo bucle cada %ss", interval)
        while True:
            try:
                run_once(cfg, client, dry_run=args.dry_run)
            except Exception:  # el daemon no debe morir por un fallo puntual
                log.exception("Error inesperado en la pasada")
            time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
