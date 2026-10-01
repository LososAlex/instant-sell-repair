"""Телеграм-алерты на голом requests (без aiogram — для воркера хватает одного POST).

send_message(text, thread_key): шлёт в TELEGRAM_CHAT_ID, в топик по THREAD_IDS.
Поддержан свой Bot API-сервер (TELEGRAM_API_BASE). Пустой токен / ошибка — no-op,
воркер не падает.
"""
import logging

import requests

import config


def send_message(text, thread_key):
    if not config.TELEGRAM_BOT_TOKEN:
        return
    base = (config.TELEGRAM_API_BASE or 'https://api.telegram.org').rstrip('/')
    data = {
        'chat_id': config.TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
    }
    thread_id = config.THREAD_IDS.get(thread_key)
    if thread_id is not None:
        data['message_thread_id'] = thread_id
    try:
        r = requests.post(f'{base}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage', json=data, timeout=10)
        if not r.ok:
            # 400/403/429 раньше терялись молча: сообщение не доходило, а в логе пусто
            logging.error('[telegram] HTTP %s %s', r.status_code, r.text[:300])
    except Exception as e:
        logging.error('[telegram] send failed: %s', e)
