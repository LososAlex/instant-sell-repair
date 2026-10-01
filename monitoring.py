"""События в сервис мониторинга -> Grafana (БД bot_events).

Тот же механизм, что в прод-ФС (FS/monitor-bots-fs/monitoring.py): POST на
{MONITORING_URL}/event с заголовком x-api-key. Каждое событие тегируется
stage=config.STAGE ('mm2-swap'), чтобы в Grafana отделять своп от ФС.
Если MONITORING_URL пуст — no-op (воркер работает без телеметрии).
"""
import logging

import requests

import config


def send_event(username, event_type, payload=None):
    if not config.MONITORING_URL:
        return
    try:
        requests.post(
            f'{config.MONITORING_URL}/event',
            json={
                'username': username,
                'event_type': event_type,
                'payload': {'stage': config.STAGE, **(payload or {})},
            },
            headers={'x-api-key': config.MONITORING_KEY},
            timeout=8,
        )
    except Exception as e:
        logging.error('[monitoring] send failed: %s', e)
