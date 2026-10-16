# agente-padel

Monitor de pistas de pádel libres en **Coslada Dreamfit** (Playtomic). Avisa por Telegram de las pistas libres de **lunes a viernes**, en los **próximos 12 días**, que **empiezan entre las 18:30 y las 21:00** y duran **90 minutos**.

- La API devuelve `start_time` en **UTC**. Se convierte a hora de Madrid con `zoneinfo`, que respeta el cambio de hora, y nunca sumando un número fijo de horas. Comprobado con datos reales: el turno de las 21:30 llega como 19:30 en horario de verano y como 20:30 en horario de invierno.
- Los nombres de las pistas ("Padel 7"…) se sacan cruzando el `resource_id` con la lista de pistas que trae la propia web del club, nunca por la posición en el array. Si alguno no se resuelve, se puede forzar con `COURT_NAMES`.
- Anti-spam: `state.json` guarda los slots libres de la pasada anterior y solo se avisa de los nuevos. Si una pista se reserva y luego se vuelve a liberar, se avisa otra vez.

## Probar en local (macOS)

```bash
git clone https://github.com/alejandropascualdelcid/agente-padel.git
cd agente-padel
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env        # rellena TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
pytest -q                   # tests con la API simulada

python monitor_padel.py --once --dry-run   # consulta real, imprime el mensaje sin enviarlo
python monitor_padel.py --once             # envía a Telegram
python monitor_padel.py --loop --interval 60
```

## GitHub Actions (24/7, gratis en un repo público)

1. Ve a *Settings → Secrets and variables → Actions → New repository secret* y crea `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
   O hazlo desde la terminal con `gh`:
   ```bash
   gh secret set TELEGRAM_BOT_TOKEN
   gh secret set TELEGRAM_CHAT_ID
   ```
2. El workflow `.github/workflows/monitor.yml` se ejecuta cada 5 minutos y guarda el estado con `actions/cache`. Para lanzarlo a mano: *Actions → Monitor pádel → Run workflow*, o `gh workflow run monitor.yml`.

Ten en cuenta que:
- El cron de GitHub es *best effort*: a menudo se retrasa entre 5 y 15 minutos.
- En repos públicos, GitHub desactiva los workflows programados tras 60 días sin actividad. Basta con reactivarlo desde la pestaña Actions.
- Si Cloudflare responde 403 desde los runners, ejecútalo en local o en Docker desde una IP residencial.

## Docker (Raspberry Pi, NAS, VPS…)

```bash
docker build -t agente-padel .
docker run -d --name padel --restart unless-stopped --env-file .env -v padel-data:/data agente-padel
docker logs -f padel
```
