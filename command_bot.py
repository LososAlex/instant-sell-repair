"""Команды боту в телеге (long-poll getUpdates в отдельном потоке).

/status (или /broken) -> текущие брокены: имя | reason | активный тикет | статус тикета,
плюс когда был последний тик цикла и через сколько ожидать следующий.

Плоский requests, без aiogram. Отвечает в тот же чат/топик, откуда пришла команда
(работает и в личке с ботом, и в топике general). Один процесс-потребитель getUpdates:
не запускай две копии воркера — Telegram отдаёт 409 Conflict.
"""
import logging
import threading
import time
from collections import Counter
from datetime import datetime

import requests

import config
import repair_queue as rq

HEADERS = {'x-bots-admin-key': config.ADMIN_KEY}
_LAST_TICK = {'at': None}  # UTC время конца последнего тика (обновляет воркер)


def mark_tick():
    _LAST_TICK['at'] = datetime.utcnow()


def _base():
    return (config.TELEGRAM_API_BASE or 'https://api.telegram.org').rstrip('/')


def _send(chat_id, text, thread_id=None):
    data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if thread_id is not None:
        data['message_thread_id'] = thread_id
    try:
        requests.post(f'{_base()}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage', json=data, timeout=15)
    except Exception as e:
        logging.error('[command_bot] send failed: %s', e)


def _tick_line():
    last = _LAST_TICK['at']
    if last is None:
        return 'Тик ещё не проходил (воркер только стартовал).'
    ago = int((datetime.utcnow() - last).total_seconds())
    left = max(0, config.POLL_INTERVAL_SEC - ago)
    return f'Последний тик: {ago}с назад · следующий ~через {left}с (интервал {config.POLL_INTERVAL_SEC}с). Время UTC.'


def _status_report(client):
    try:
        r = requests.get(config.API_URL + '/broken', headers=HEADERS, timeout=config.REQUEST_TIMEOUT_SEC)
    except Exception as e:
        return f'Ошибка запроса /broken: {e}'
    if r.status_code != 200:
        return f'/broken -> HTTP {r.status_code}'
    bots = r.json().get('data') or []
    col = client[config.MONGO_DB][config.MONGO_COLLECTION]

    rows = []
    for b in bots:
        name = b.get('roblox_nickname') or '?'
        game = (config.GAMES.get(b.get('game')) or {}).get('label') or f'game={b.get("game")}'
        btype = b.get('type') or '—'
        reason = b.get('reason') or '—'
        rid = b.get('roblox_account_id')
        doc = col.find_one({'roblox_id': {'$in': [int(rid), str(rid)]}}) if rid else None
        if not doc:
            rows.append((game, name, btype, reason, '—', '—', 'нет в mongo'))
            continue
        last = rq.last_ticket(client, doc['_id'])
        if not last:
            rows.append((game, name, btype, reason, 'нет', '—', 'нет тикета'))
        else:
            active = 'да' if last['status'] in rq.ACTIVE_STATUSES else 'нет'
            rows.append((game, name, btype, reason, active, last.get('reason') or '—', last['status']))

    by_game = Counter(r[0] for r in rows)
    header = (f'🩺 Брокены: {len(bots)} ('
              + ' · '.join(f'{g} {n}' for g, n in by_game.most_common()) + f')\n{_tick_line()}')
    if not rows:
        return header + '\n\nСписок пуст.'

    shown = sorted(rows)[:40]   # по игре, внутри — по нику
    w_name = min(20, max(4, *(len(r[1]) for r in shown)))
    w_type = min(10, max(4, *(len(r[2]) for r in shown)))
    w_reason = min(26, max(6, *(len(r[3]) for r in shown)))
    w_treason = min(18, max(6, *(len(r[5]) for r in shown)))
    lines = [f'{"игра":<5} | {"бот":<{w_name}} | {"type":<{w_type}} | {"reason":<{w_reason}} | актив | {"тикет-reason":<{w_treason}} | статус']
    for game, name, btype, reason, active, treason, status in shown:
        lines.append(f'{game[:5]:<5} | {name[:w_name]:<{w_name}} | {btype[:w_type]:<{w_type}} | {reason[:w_reason]:<{w_reason}} | {active:<5} | {treason[:w_treason]:<{w_treason}} | {status}')
    table = '<pre>' + '\n'.join(lines) + '</pre>'
    tail = f'\n… и ещё {len(rows) - 40}' if len(rows) > 40 else ''
    return header + '\n' + table + tail


def _poll_loop(client):
    offset = None
    while True:
        try:
            params = {'timeout': 25}
            if offset is not None:
                params['offset'] = offset
            r = requests.get(f'{_base()}/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates', params=params, timeout=35)
            for upd in r.json().get('result', []):
                offset = upd['update_id'] + 1
                msg = upd.get('message') or {}
                chat = msg.get('chat') or {}
                thread_id = msg.get('message_thread_id')
                cmd = (msg.get('text') or '').strip().split('@')[0].lower()
                if cmd in ('/status', '/broken'):
                    _send(chat.get('id'), _status_report(client), thread_id)
                elif cmd in ('/help', '/start'):
                    _send(chat.get('id'), 'Команды:\n/status — брокены + тикеты + время тика', thread_id)
        except Exception as e:
            logging.error('[command_bot] poll error: %r', e)
            time.sleep(5)


def start(client):
    if not config.TELEGRAM_BOT_TOKEN:
        logging.info('[command_bot] токена нет — слушатель команд не запущен')
        return
    threading.Thread(target=_poll_loop, args=(client,), daemon=True).start()
    logging.info('[command_bot] слушатель команд запущен (getUpdates)')
