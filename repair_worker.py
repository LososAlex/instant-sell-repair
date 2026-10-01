"""Воркер починки ботов mm2-swap с тикет-системой (repair_queue).

Каждый цикл: GET /bots/broken ->
  сначала INVENTORY с незакрытым тикетом moderated -> DEAD (kill_moderated, ключ CV),
  их дальше не чиним; по остальным ботам (как в прод-ФС):
  - есть активный тикет (queued/in_progress) -> пропуск, его чинят;
  - последний тикет fixed (воркер починки обновил accounts_p2p) -> до-применяем repair:
        куки  -> repair с cookies[0] (свежие из accounts_p2p)
        капча -> repair без данных (только bot_id)
    статус тикета НЕ меняем (как в ФС); бот после repair уходит из брокенов;
  - тикета нет/failed -> обычный путь по state:
        BROKEN, cookies >= 2 -> repair с cookies[1], при успехе удаляем cookies[0];
                                если repair != 200 -> retry на следующих циклах,
                                после MAX_REPAIR_ATTEMPTS -> тикет refresh_invalid;
        BROKEN, cookies < 2  -> тикет refresh_invalid;
        CAPTCHA              -> тикет captcha.

Куки берём из accounts_p2p. Прод-uuid для /repair берём из /broken (поле bot_id).
enable после repair НЕ делаем — это сторона бэка. Логи -> logs/repair.log (ротация).
Настройки — config.py, тикеты — repair_queue.py.
"""
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import pymongo
import requests

import command_bot as cmd
import config
import monitoring as mon
import notifications as tg
import repair_queue as rq

HEADERS = {'x-bots-admin-key': config.ADMIN_KEY}

# bot _id -> число подряд неудачных авто-починок (repair != 200). В памяти процесса.
_repair_attempts = {}


def setup_logging():
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, 'repair.log'), maxBytes=5_000_000, backupCount=5, encoding='utf-8')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[file_handler, logging.StreamHandler()])


def cookies_col(client):
    return client[config.MONGO_DB][config.MONGO_COLLECTION]


def repair(items):
    r = requests.patch(config.API_URL + '/repair', json={'items': items},
                       headers=HEADERS, timeout=config.REQUEST_TIMEOUT_SEC)
    logging.info('repair: %s %s', r.status_code, r.text[:300])
    return r.status_code == 200


def game_of(b):
    """/broken отдаёт ботов ВСЕХ игр свопа (game — число enum Games). Метка — в телеграм,
    stage — в Grafana, тот же, что у ротатора этой игры. Незнакомая игра видна как game=N."""
    return config.GAMES.get(b.get('game')) or {'label': f'game={b.get("game")}',
                                                'stage': config.STAGE}


def ev(b, **payload):
    """payload события: stage игры бота перекрывает общий STAGE в monitoring.send_event."""
    g = game_of(b)
    return {'stage': g['stage'], 'game': g['label'], **payload}


def tag(b):
    return f'[{game_of(b)["label"]}]'


def apply_fixed(client, b, doc, ticket, name):
    """Тикет fixed: воркер починки обновил accounts_p2p -> до-применяем repair.
    После успеха тикет -> resolved (и капча, и куки), чтобы повторный слом
    трактовался как свежий инцидент, а не зацикливал применение старого тикета."""
    if ticket.get('reason') == rq.REASON_CAPTCHA:
        if repair([{'bot_id': b['bot_id']}]):  # капча: без данных
            rq.resolve_ticket(client, ticket['_id'])
            mon.send_event(name, 'bot_captcha_repaired', ev(b, mode='ticket_fixed'))
            tg.send_message(f'{tag(b)} <b>{name}</b> капча-тикет решён, бот восстановлен', 'captcha_in_roblox')
        return
    cookies = doc.get('cookies') or []
    if not cookies:
        logging.warning('тикет fixed, но cookies пусты: %s', name)
        return
    if repair([{'bot_id': b['bot_id'], 'roblox_auth': cookies[0]}]):  # свежая cookies[0]
        rq.resolve_ticket(client, ticket['_id'])  # закрываем -> повторный слом = свежий инцидент
        mon.send_event(name, 'bot_cookie_updated', ev(b, mode='ticket_fixed'))
        tg.send_message(f'{tag(b)} <b>{name}</b> тикет решён, кука обновлена в проде', 'refresh_updated')


def auto_repair_cookie(client, b, _id, name, cookies):
    """BROKEN, cookies>=2: чиним cookies[1]. При успехе ротация. !=200 -> retry на следующих
    циклах; после MAX_REPAIR_ATTEMPTS подряд -> алерт в general и продолжаем пробовать."""
    if repair([{'bot_id': b['bot_id'], 'roblox_auth': cookies[1]}]):
        cookies_col(client).update_one({'_id': _id}, {'$set': {'cookies': cookies[1:]}})
        _repair_attempts.pop(_id, None)
        mon.send_event(name, 'bot_cookie_updated', ev(b, mode='auto_refresh'))
        tg.send_message(f'{tag(b)} <b>{name}</b> обновлена кука (авто, cookies[1])', 'refresh_updated')
        return
    n = _repair_attempts.get(_id, 0) + 1
    logging.warning('auto-repair !=200 для %s, попытка %d/%d', name, n, config.MAX_REPAIR_ATTEMPTS)
    if n >= config.MAX_REPAIR_ATTEMPTS:
        _repair_attempts.pop(_id, None)  # сброс — пробуем снова на следующих циклах
        tg.send_message(f'{tag(b)} <b>{name}</b> авто-починка кук не удалась {config.MAX_REPAIR_ATTEMPTS} раза подряд, пробуем дальше', 'general')
    else:
        _repair_attempts[_id] = n


_alerted_reasons = set()  # новые reason, о которых уже сообщили в general (без спама)
_alerted_bots = set()     # (reason, bot_id), по которым уже дали алерт — чтобы не спамить каждый цикл


def _alert_once(reason, bot_id, chat_key, text):
    """TG-алерт один раз за жизнь процесса по (reason, bot_id).
    ponytail: in-memory, сбрасывается на рестарте — тогда алертнёт заново, это ок."""
    key = (reason, bot_id)
    if key in _alerted_bots:
        return
    _alerted_bots.add(key)
    tg.send_message(text, chat_key)


def open_captcha_ticket(client, b, _id, name):
    if rq.create_ticket(client, _id, name, rq.REASON_CAPTCHA):
        mon.send_event(name, 'bot_in_roblox_captcha', ev(b))
        tg.send_message(f'{tag(b)} <b>{name}</b> словил капчу, тикет на ручную починку', 'captcha_in_roblox')


def handle_broken(client, b):
    """Один бот из /broken. Диспатч по reason (см. config)."""
    name = b.get('roblox_nickname')
    reason = b.get('reason')
    if reason not in config.KNOWN_REASONS:
        # бэк добавил новый reason -> алерт разово, бота не трогаем
        if reason not in _alerted_reasons:
            _alerted_reasons.add(reason)
            logging.warning('новый reason в /broken: %s (бот %s)', reason, name)
            tg.send_message(f'⚠️ Новый reason в /broken: <b>{reason}</b> (бот {tag(b)} {name}) — воркер не обрабатывает', 'general')
        return
    if reason in config.SKIP_REASONS:
        logging.info('скип по reason=%s: %s', reason, name)
        return

    # reason'ы без mongo/тикетов
    if reason == config.REASON_BANNED:
        logging.info('бан, скип: %s', name)
        mon.send_event(name, 'bot_banned', ev(b))
        _alert_once(reason, b.get('bot_id'), 'general',
                    f'{tag(b)} <b>{name}</b> забанен (LNKD_RBX_ACCOUNT_IS_BANNED) — не восстанавливаем')
        return
    if reason == config.REASON_NO_PRVT:
        logging.info('нет доступа к приват-серверу: %s', name)
        mon.send_event(name, 'bot_bad_private', ev(b))
        _alert_once(reason, b.get('bot_id'), 'general',
                    f'{tag(b)} <b>{name}</b> нет доступа к приватному серверу — проверить VIP-ссылку')
        return
    if reason in config.REPAIR_ONLY_REASONS:
        repair([{'bot_id': b['bot_id']}])  # просто дёргаем ручку без кук
        return

    # дальше — reason'ы с починкой по кукам / капчей: нужны mongo-док и тикет-логика
    rid = b['roblox_account_id']
    # roblox_id в mongo хранится числом; /broken отдаёт строку -> матчим оба типа
    doc = cookies_col(client).find_one({'roblox_id': {'$in': [int(rid), str(rid)]}})
    if not doc:
        logging.warning('нет в accounts_p2p: %s', b.get('roblox_account_id'))
        mon.send_event(name, 'bot_not_found_mongo', ev(b, roblox_id=str(b.get('roblox_account_id'))))
        tg.send_message(f'{tag(b)} <b>{name}</b> не найден в accounts_p2p (roblox_id {b.get("roblox_account_id")})', 'broken_cookie')
        return
    name = doc.get('username') or name
    _id = doc['_id']

    last = rq.last_ticket(client, _id)
    if last and last['status'] in rq.ACTIVE_STATUSES:
        return  # уже чинят
    if last and last['status'] == 'fixed':
        apply_fixed(client, b, doc, last, name)
        return

    if reason in config.CAPTCHA_REASONS:
        open_captcha_ticket(client, b, _id, name)
        return

    # COOKIE_REASONS (None, TRADE_INVALID_BOT_COOKIE): диспатч по state, как раньше
    state = b.get('state')
    if state == 'BROKEN':
        cookies = doc.get('cookies') or []
        if len(cookies) < 2:
            if rq.create_ticket(client, _id, name, rq.REASON_REFRESH_INVALID):
                mon.send_event(name, 'bot_broken_refresh', ev(b, reason='cookies<2'))
                tg.send_message(f'{tag(b)} <b>{name}</b> сломаны рефреш-куки (cookies&lt;2), тикет на обновление', 'broken_refresh')
            return
        auto_repair_cookie(client, b, _id, name, cookies)
    elif state == 'CAPTCHA':
        open_captcha_ticket(client, b, _id, name)
    else:
        logging.info('пропуск, неизвестный state: %s %s', state, b.get('bot_id'))


def pick_moderated(bots, moderated, cap):
    """Кого списать: INVENTORY из /broken, чей ник в moderated (незакрытый тикет).
    TRADE не трогаем — на нём сток. -> (до cap ботов, сколько кандидатов всего)."""
    hits = [b for b in bots
            if b.get('type') == 'INVENTORY' and b.get('roblox_nickname') in moderated]
    return hits[:cap], len(hits)


def mark_dead(b):
    r = requests.patch(f'{config.API_URL}/{b["roblox_account_id"]}/dead',
                       headers={'x-cv-key': config.CV_KEY}, timeout=config.REQUEST_TIMEOUT_SEC)
    logging.info('dead %s: %s %s', b.get('roblox_nickname'), r.status_code, r.text[:300])
    return r.status_code == 200


def kill_moderated(client, bots):
    """Замодеренный аккаунт не чинится ничем, а BROKEN держит место в лимите парка —
    ротатор не может залить замену. Поэтому DEAD. Стока у INVENTORY нет (01.10: 0 штук
    на 180 живых), откат — PATCH /bots/repair. Тикет moderated после DEAD закрываем (resolved).
    Возвращает bot_id списанных — их дальше не чиним."""
    if not config.CV_KEY:
        return set()
    moderated = rq.open_moderated(client, {b.get('roblox_nickname') for b in bots})
    hits, total = pick_moderated(bots, moderated, config.MAX_DEAD_PER_TICK)
    if total > len(hits):
        tg.send_message(f'⚠️ Замодеренных INVENTORY на списание {total} — больше предохранителя '
                        f'{config.MAX_DEAD_PER_TICK}, за цикл беру {len(hits)}', 'general')
    dead = []
    for b in hits:
        name = b.get('roblox_nickname')
        try:
            ok = mark_dead(b)
        except Exception as e:
            logging.error('dead %s: %r', name, e)
            ok = False
        if not ok:
            _alert_once('dead_failed', b.get('bot_id'), 'general',
                        f'{tag(b)} <b>{name}</b> замодерен, но DEAD не поставился — см. logs/repair.log')
            continue
        dead.append(b)
        rq.resolve_moderated(client, name)
        mon.send_event(name, 'bot_dead', ev(b, reason=rq.REASON_MODERATED, type='INVENTORY'))
    if dead:
        tg.send_message(f'Замодеренные INVENTORY → DEAD ({len(dead)}):\n'
                        + ''.join(f'{tag(b)} <b>{b.get("roblox_nickname")}</b>\n' for b in dead),
                        'general')
    return {b.get('bot_id') for b in dead}


_prev_broken = set()


def notify_new(bots):
    """Алерт о ботах, появившихся в брокенах с прошлого цикла."""
    global _prev_broken
    current = {b.get('roblox_nickname'): b for b in bots}
    new = sorted((n for n in current if n not in _prev_broken), key=lambda n: tag(current[n]))
    _prev_broken = set(current)
    if not new:
        return
    txt = f'Новые брокен-боты в свопе ({len(new)}):\n'
    txt += ''.join(f'{tag(current[n])} <b>{n}</b>\n' for n in new[:50])
    if len(new) > 50:
        txt += f'... и ещё {len(new) - 50}\n'
    tg.send_message(txt, 'new_brokens_in_prod')


def tick(client):
    r = requests.get(config.API_URL + '/broken', headers=HEADERS, timeout=config.REQUEST_TIMEOUT_SEC)
    if r.status_code != 200:
        logging.error('broken: HTTP %s %s', r.status_code, r.text[:300])
        return
    bots = r.json().get('data') or []
    notify_new(bots)
    if not bots:
        logging.info('брокенов нет')
        return
    try:
        dead = kill_moderated(client, bots)
    except Exception as e:  # списание не должно ломать починку остальных
        logging.error('kill_moderated error: %r', e)
        dead = set()
    for b in bots:
        if b.get('bot_id') in dead:
            continue
        try:
            handle_broken(client, b)
        except Exception as e:
            logging.error('handle_broken error: %r', e)


def selfcheck():
    bots = [{'roblox_nickname': 'a', 'type': 'INVENTORY'}, {'roblox_nickname': 'b', 'type': 'TRADE'},
            {'roblox_nickname': 'c', 'type': 'INVENTORY'}]
    assert pick_moderated(bots, {'a', 'b'}, 20) == ([bots[0]], 1), 'TRADE не списываем'
    assert pick_moderated(bots, {'a', 'c'}, 1) == ([bots[0]], 2), 'предохранитель'
    assert pick_moderated(bots, set(), 20) == ([], 0)
    # игра бота: метка в телеграм, stage в Grafana; незнакомая — общий STAGE
    mm2, adopt = config.GAMES[4], config.GAMES[0]   # метки из конфига: у копии IS свои
    assert ev({'game': 4}, mode='x') == {'stage': mm2['stage'], 'game': mm2['label'], 'mode': 'x'}
    assert tag({'game': 0}) == f'[{adopt["label"]}]' and tag({'game': 7}) == '[game=7]'
    assert ev({'game': 7})['stage'] == config.STAGE
    print('selfcheck ok')


def main():
    if '--selfcheck' in sys.argv:
        selfcheck()
        return
    setup_logging()
    client = pymongo.MongoClient(config.MONGO_URI)
    cmd.start(client)  # слушатель команд /status в телеге (отдельный поток)
    logging.info('repair-worker (tickets) запущен, интервал %d с', config.POLL_INTERVAL_SEC)
    while True:
        try:
            tick(client)
        except Exception as e:  # воркер не должен падать на одной итерации
            logging.error('tick error: %r', e)
        cmd.mark_tick()  # отметка времени завершённого цикла для /status
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == '__main__':
    main()
