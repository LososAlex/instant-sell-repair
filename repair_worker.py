"""Воркер починки ботов mm2-swap с тикет-системой (repair_queue).

Каждый цикл: GET /bots/broken ->
  сначала INVENTORY с незакрытым тикетом moderated -> DEAD (kill_moderated, ключ CV),
  потом TRADE/INVENTORY с капчей и 0 предметов -> DEAD (kill_captcha_empty, ключ CV + БД бэка),
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
  NO_ACCESS_TO_PRVT_SERVER -> repair с VIP-ссылкой и кодом приватки из записи свопа
  (mm2_swap / adopt_swap); у MM2 только при живом VIP (vip_server_until): истёк — алерт,
  не куплен — покупаем через Worker3 (buy_vip_for_prod) и чиним новой ссылкой.

Куки берём из accounts_p2p. Прод-uuid для /repair берём из /broken (поле bot_id).
enable после repair НЕ делаем — это сторона бэка. Логи -> logs/repair.log (ротация).
Настройки — config.py, тикеты — repair_queue.py.
"""
import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

import psycopg2
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
    if rq.create_ticket(client, _id, name, rq.REASON_CAPTCHA, game_of(b).get('service')):
        mon.send_event(name, 'bot_in_roblox_captcha', ev(b))
        tg.send_message(f'{tag(b)} <b>{name}</b> словил капчу, тикет на ручную починку', 'captcha_in_roblox')


_prvt_repaired = {}  # bot_id -> когда переотправили приватку (time.time()). В памяти процесса
_vip_tried = {}      # bot_id -> когда пробовали купить VIP. В памяти процесса

VIP_NOT_BOUGHT = 'VIP не куплен'
PRVT_FIELDS = {'roblox_link': 1, 'access_code': 1, 'vip_server_until': 1, 'username': 1,
               'status': 1, 'robux_balance': 1, 'mm2_trade_access': 1}


def prvt_problem(doc, vip_required, now):
    """Почему приватку не чиним; None — чиним. doc — запись свопа (roblox_link, access_code,
    vip_server_until). VIP_NOT_BOUGHT — не приговор: repair_private попробует купить."""
    if not doc:
        return 'нет записи в коллекции свопа'
    if vip_required:
        until = doc.get('vip_server_until')
        if not until:
            return VIP_NOT_BOUGHT
        if until <= now:
            return f'VIP истёк {until:%d.%m} — нужна докупка'
    if not (doc.get('roblox_link') and doc.get('access_code')):
        return 'нет VIP-ссылки или кода приватки'
    return None


def vip_gate_problem(doc, min_balance):
    """Почему Worker3 не купит VIP этому боту (гейт: прогрет, баланс); None — можно звать."""
    if not doc.get('mm2_trade_access'):
        return 'бот не прогрет (mm2_trade_access)'
    if (doc.get('robux_balance') or 0) < min_balance:
        return f'баланс {doc.get("robux_balance") or 0} R$ < {min_balance}'
    return None


def buy_vip_for_prod(coll, doc):
    """Купить VIP боту в проде через Worker3. -> None (куплен) или текст, почему нет.

    Гейт Worker3 пускает только status=unready и после покупки ставит ready, поэтому на время
    вызова переводим бота prod -> unready и в finally возвращаем prod (решение Алексея 14.10).
    Окно ~5-15 с: ротатор в нём может принять бота за запас и залить — его POST /bots вернёт
    BOT_ALREADY_EXISTS, и ротатор сам поставит prod. Тикеты бота гейт тоже смотрит -> skipped."""
    if not config.VIP_API_KEY:
        return 'VIP_API_KEY не задан'
    gate = vip_gate_problem(doc, config.VIP_MIN_BALANCE)
    if gate:
        return f'Worker3 не купит: {gate}'
    if not coll.update_one({'_id': doc['_id'], 'status': 'prod'},
                           {'$set': {'status': 'unready'}}).modified_count:
        return f'статус бота {doc.get("status")}, не prod — не трогаю'
    try:
        r = requests.post(config.VIP_HUB_URL, json={'usernames': [doc['username']]},
                          headers={'API-KEY': config.VIP_API_KEY}, timeout=config.VIP_TIMEOUT_SEC)
        data = r.json() if r.ok else {}
        logging.info('buy-vip %s: %s %s', doc['username'], r.status_code, r.text[:300])
    except Exception as e:
        return f'Worker3: {e!r}'[:200]
    finally:
        coll.update_one({'_id': doc['_id'], 'status': {'$in': ['unready', 'ready']}},
                        {'$set': {'status': 'prod'}})
    if any(x.get('ok') for x in data.get('results') or []):
        return None
    return f'Worker3 не купил: {(data.get("skipped") or data.get("results") or r.text[:200])}'


def repair_private(client, b, name):
    """NO_ACCESS_TO_PRVT_SERVER -> заново шлём в прод VIP-ссылку и код приватки из записи свопа
    (repair с private_server_link + access_code, кука не трогается). MM2 — только при живом VIP
    (vip_server_until в будущем): истёк — алерт; не куплен — покупаем (buy_vip_for_prod) и
    чиним новой ссылкой. Adopt — как есть."""
    g = game_of(b)
    rid = b['roblox_account_id']
    doc, coll, bought = None, None, False
    if g.get('collection'):
        coll = client[config.MONGO_DB][g['collection']]
        doc = coll.find_one({'roblox_id': {'$in': [int(rid), str(rid)]}}, PRVT_FIELDS)
    why = prvt_problem(doc, g.get('vip_required'), datetime.utcnow())
    if why == VIP_NOT_BOUGHT:
        if time.time() - _vip_tried.get(b['bot_id'], 0) < config.PRVT_REPAIR_COOLDOWN_SEC:
            return  # покупать пробовали меньше часа назад, алерт уже был
        _vip_tried[b['bot_id']] = time.time()
        fail = buy_vip_for_prod(coll, doc)
        if fail:
            why = f'{VIP_NOT_BOUGHT}, купить не вышло: {fail}'
        else:
            bought = True
            mon.send_event(name, 'bot_vip_bought', ev(b))
            doc = coll.find_one({'_id': doc['_id']}, PRVT_FIELDS)
            why = prvt_problem(doc, g.get('vip_required'), datetime.utcnow())
    if why:
        mon.send_event(name, 'bot_bad_private', ev(b, reason=why))
        _alert_once(config.REASON_NO_PRVT, b.get('bot_id'), 'general',
                    f'{tag(b)} <b>{name}</b> нет доступа к приватному серверу, не чиню: {why}')
        return
    last = _prvt_repaired.get(b['bot_id'])
    if last and time.time() - last < config.PRVT_REPAIR_COOLDOWN_SEC:
        _alert_once('prvt_again', b.get('bot_id'), 'general',
                    f'{tag(b)} <b>{name}</b> приватку переотправили меньше часа назад, а доступа снова '
                    f'нет — проверить VIP-ссылку вручную')
        return
    if repair([{'bot_id': b['bot_id'], 'private_server_link': doc['roblox_link'],
                'access_code': doc['access_code']}]):
        _prvt_repaired[b['bot_id']] = time.time()
        until = doc.get('vip_server_until')
        mon.send_event(name, 'bot_private_repaired', ev(b, vip_bought=bought))
        tg.send_message(f'{tag(b)} <b>{name}</b> нет доступа к приватке — '
                        + ('купили VIP и отправили' if bought else 'переотправили')
                        + ' VIP-ссылку и код' + (f' (VIP до {until:%d.%m})' if until else ''), 'general')


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
        repair_private(client, b, name)
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


def dead_batch(client, hits, total, reason, title):
    """hits -> DEAD, их незакрытые тикеты reason закрываем (resolved). total — кандидатов
    всего: сверх предохранителя пишем в general. Откат DEAD — PATCH /bots/repair.
    Возвращает bot_id списанных — их дальше не чиним."""
    if total > len(hits):
        tg.send_message(f'⚠️ {title}: на списание {total} — больше предохранителя '
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
                        f'{tag(b)} <b>{name}</b> на списание ({title}), но DEAD не поставился — '
                        f'см. logs/repair.log')
            continue
        dead.append(b)
        rq.resolve_open(client, reason, name, f'bot DEAD in swap prod ({reason}) by swap repair-worker')
        mon.send_event(name, 'bot_dead', ev(b, reason=reason, type=b.get('type')))
    if dead:
        tg.send_message(f'{title} → DEAD ({len(dead)}):\n'
                        + ''.join(f'{tag(b)} <b>{b.get("roblox_nickname")}</b>\n' for b in dead),
                        'general')
    return {b.get('bot_id') for b in dead}


def kill_moderated(client, bots):
    """Замодеренный аккаунт не чинится ничем, а BROKEN держит место в лимите парка —
    ротатор не может залить замену. Поэтому DEAD. Стока у INVENTORY нет (01.10: 0 штук
    на 180 живых). Возвращает bot_id списанных."""
    if not config.CV_KEY:
        return set()
    moderated = rq.open_tickets(client, rq.REASON_MODERATED, {b.get('roblox_nickname') for b in bots})
    hits, total = pick_moderated(bots, moderated, config.MAX_DEAD_PER_TICK)
    return dead_batch(client, hits, total, rq.REASON_MODERATED, 'Замодеренные INVENTORY')


def empty_bots(rids):
    """roblox_account_id из rids, у которых в БД бэка нет ни одного предмета (inventory_items;
    на 14.10 bots.items_count с ним совпадал у всех капча-ботов)."""
    conn = psycopg2.connect(config.SWAPS_PG_DSN)
    try:
        cur = conn.cursor()
        cur.execute('select b.roblox_account_id::text from bots b '
                    'where b.roblox_account_id::text = any(%s) and not exists ('
                    'select 1 from inventory_items i where i.roblox_account_id = b.roblox_account_id)',
                    (list(rids),))
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def pick_captcha_empty(bots, ticketed, empty, cap):
    """Кого списать: капча (reason из /broken или незакрытый тикет captcha по нику) и 0 предметов
    (roblox_account_id в empty), только CAPTCHA_EMPTY_TYPES. -> (до cap ботов, кандидатов всего)."""
    hits = [b for b in bots
            if b.get('type') in config.CAPTCHA_EMPTY_TYPES
            and (b.get('reason') in config.CAPTCHA_REASONS or b.get('state') == 'CAPTCHA'
                 or b.get('roblox_nickname') in ticketed)
            and str(b.get('roblox_account_id')) in empty]
    return hits[:cap], len(hits)


def kill_captcha_empty(client, bots):
    """Капча + 0 предметов -> DEAD (Алексей, 14.10): чинить капчу руками ради пустого бота
    незачем, а BROKEN держит место в лимите парка. Стока нет — терять нечего, ротатор
    переведёт запись в done и зальёт замену. Возвращает bot_id списанных."""
    if not (config.CV_KEY and config.SWAPS_PG_DSN):
        return set()
    ticketed = rq.open_tickets(client, rq.REASON_CAPTCHA, {b.get('roblox_nickname') for b in bots})
    empty = empty_bots({str(b.get('roblox_account_id')) for b in bots})
    hits, total = pick_captcha_empty(bots, ticketed, empty, config.MAX_DEAD_PER_TICK)
    return dead_batch(client, hits, total, rq.REASON_CAPTCHA, 'Капча, 0 предметов')


_prev_broken = set()


def notify_new(bots):
    """Алерт о ботах, появившихся в брокенах с прошлого цикла."""
    global _prev_broken
    current = {b.get('roblox_nickname'): b for b in bots}
    new = sorted((n for n in current if n not in _prev_broken), key=lambda n: tag(current[n]))
    _prev_broken = set(current)
    if not new:
        return
    # заголовок у каждого сервиса свой (своп / Instant Sell). getattr — чтобы старый серверный
    # config.py без этого ключа не ронял тик: деплой свопа — копированием файлов руками
    txt = f'{getattr(config, "BROKEN_TITLE", "Новые брокен-боты в свопе")} ({len(new)}):\n'
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
    dead = set()
    for kill in (kill_moderated, kill_captcha_empty):
        try:
            dead |= kill(client, [b for b in bots if b.get('bot_id') not in dead])
        except Exception as e:  # списание не должно ломать починку остальных
            logging.error('%s error: %r', kill.__name__, e)
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
    # капча + 0 предметов: reason или тикет по нику; VETERAN и боты с предметами не трогаем
    cb = [{'roblox_account_id': '1', 'type': 'TRADE', 'reason': 'TRADE_CAPTCHA_ERROR'},
          {'roblox_account_id': '2', 'type': 'TRADE', 'reason': 'TRADE_CAPTCHA_ERROR'},
          {'roblox_account_id': '3', 'type': 'VETERAN', 'reason': 'TRADE_CAPTCHA_ERROR'},
          {'roblox_account_id': '4', 'type': 'INVENTORY', 'reason': None, 'roblox_nickname': 'd'},
          {'roblox_account_id': '5', 'type': 'INVENTORY', 'reason': None, 'roblox_nickname': 'e'}]
    empty = {'1', '3', '4', '5'}
    assert pick_captcha_empty(cb, {'d'}, empty, 20) == ([cb[0], cb[3]], 2)
    assert pick_captcha_empty(cb, {'d'}, empty, 1) == ([cb[0]], 2), 'предохранитель'
    assert pick_captcha_empty(cb, set(), set(), 20) == ([], 0)
    # приватка: MM2 чиним только с живым VIP, Adopt — с тем, что есть
    now, link = datetime(2026, 10, 14), {'roblox_link': 'l', 'access_code': 'c'}
    assert prvt_problem({**link, 'vip_server_until': datetime(2026, 12, 1)}, True, now) is None
    assert 'истёк' in prvt_problem({**link, 'vip_server_until': datetime(2026, 10, 3)}, True, now)
    assert prvt_problem(link, True, now) == 'VIP не куплен'
    assert prvt_problem(link, None, now) is None, 'Adopt: срока нет — чиним'
    assert prvt_problem({'roblox_link': 'l'}, None, now) and prvt_problem(None, None, now)
    assert prvt_problem({'username': 'u'}, True, now) == VIP_NOT_BOUGHT, 'не куплен — покупаем и без ссылки'
    assert vip_gate_problem({'mm2_trade_access': True, 'robux_balance': 30}, 30) is None
    assert 'баланс 12' in vip_gate_problem({'mm2_trade_access': True, 'robux_balance': 12}, 30)
    assert 'прогрет' in vip_gate_problem({'robux_balance': 99}, 30)
    # игра бота: метка в телеграм, stage в Grafana; незнакомая — общий STAGE
    mm2, adopt = config.GAMES[4], config.GAMES[0]   # метки из конфига: у копии IS свои
    assert ev({'game': 4}, mode='x') == {'stage': mm2['stage'], 'game': mm2['label'], 'mode': 'x'}
    assert tag({'game': 0}) == f'[{adopt["label"]}]' and tag({'game': 7}) == '[game=7]'
    assert ev({'game': 7})['stage'] == config.STAGE
    assert game_of({'game': 4}).get('service') == mm2.get('service') and 'service' not in game_of({'game': 7})
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
