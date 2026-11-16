"""Гейт перед выгрузкой бота наружу: жив ли аккаунт (GET usermoderation.roblox.com/v2/not-approved).

Спека и итог A/B-теста 09–12.11.2026: https://claude.ai/artifact/DGsDyFdMfE8zykPNpu1VQA
Один файл на все сервисы (ротаторы, заливщики, мониторы, repair-worker). Канон —
roblox-research/nap_gate.py в пространстве: правишь там и копируешь в репо сервисов как есть.

Запрос только «защищённый», как в тесте: лицо профиля реги (accounts_p2p.reg_info.data.fingerprint),
curl_cffi impersonate chrome, свой прокси бота (accounts_p2p.proxy -> group_proxies; снят — живой
прокси той же группы, см. bot_proxy), без CSRF и logout, без ретраев. Нет лица или живого прокси —
бота не проверяем и наружу не пускаем.

Кука, адрес прокси и текст исключений в лог не идут — только тип исключения.
Самопроверка без сети: python nap_gate.py
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote

URL = 'https://usermoderation.roblox.com/v2/not-approved'
# ponytail: кэш результата на бота. Проба раз в час доказанно безопасна (A/B), чаще не проверяли
RECHECK = timedelta(hours=1)
PAUSE = {'challenge': 6 * 3600, 'rate_limited': 30 * 60}   # стоп ВСЕХ проверок процесса, сек
EVENT = 'bot_gate_blocked'
GREASE = ' (:-./);=?_'
PERMS = [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)]
_pause = {'until': 0.0, 'why': None}


def sec_ch_ua(major):
    """sec-ch-ua как у Chrome: GREASE-бренд и порядок зависят от мажорной версии (user_agent_utils.cc)."""
    out = [None] * 3
    brands = (f'"Not{GREASE[major % 11]}A{GREASE[(major + 1) % 11]}Brand";v="{("8", "99", "24")[major % 3]}"',
              f'"Chromium";v="{major}"', f'"Google Chrome";v="{major}"')
    for pos, brand in zip(PERMS[major % 6], brands):
        out[pos] = brand
    return ', '.join(out)


def accept_language(langs):
    codes = [m.group(1) for s in langs or [] if (m := re.match(r'\[([\w-]+)\]', str(s)))] or ['en-US']
    if len(codes) == 1 and '-' in codes[0]:
        codes.append(codes[0].split('-')[0])  # Chrome сам добавляет базовый язык
    return ','.join(c if i == 0 else f'{c};q={max(10 - i, 1) / 10:.1f}' for i, c in enumerate(codes))


def headers(fp, cookie):
    """fp = accounts_p2p.reg_info.data.fingerprint (профиль Octo). Порядок ключей — как у сайта."""
    ua = fp['user_agent']
    platform = 'macOS' if 'Macintosh' in ua else 'Windows' if 'Windows' in ua else 'Linux'
    return {
        'sec-ch-ua-platform': f'"{platform}"',
        'user-agent': ua,
        'accept': 'application/json, text/plain, */*',
        'sec-ch-ua': sec_ch_ua(int(re.search(r'Chrome/(\d+)', ua).group(1))),
        'sec-ch-ua-mobile': '?0',
        'origin': 'https://www.roblox.com',
        'sec-fetch-site': 'same-site',
        'sec-fetch-mode': 'cors',
        'sec-fetch-dest': 'empty',
        'referer': 'https://www.roblox.com/',
        'accept-encoding': 'gzip, deflate, br, zstd',
        'accept-language': accept_language((fp.get('languages') or {}).get('data')),
        'cookie': f'.ROBLOSECURITY={cookie}',
        'priority': 'u=1, i',
    }


def classify(status, hdrs, body):
    """hdrs — ключи в нижнем регистре."""
    if any(k.startswith('rblx-challenge-') and v for k, v in hdrs.items()):
        return 'challenge'
    if status == 200:
        if isinstance(body, dict) and 'restriction' in body and 'errors' not in body:
            r = body['restriction']
            return 'clean' if r is None else 'restricted' if isinstance(r, dict) else 'unexpected'
        return 'unexpected'
    if status == 403:
        return 'csrf_403' if hdrs.get('x-csrf-token') else 'forbidden'
    return {401: 'unauthorized', 429: 'rate_limited'}.get(status, f'http_{status}')


def check(fp, cookie, proxy_url):
    """Один запрос через прокси бота. -> (исход, restriction или None, новая кука или None)."""
    from curl_cffi import requests as cr  # curl_cffi==0.16.3; импорт здесь — самопроверке сеть не нужна
    r = cr.get(URL, headers=headers(fp, cookie), impersonate='chrome', default_headers=False,
               proxies={'http': proxy_url, 'https': proxy_url}, timeout=20, allow_redirects=False)
    hdrs = {k.lower(): v for k, v in r.headers.items()}
    try:
        body = r.json()
    except Exception:
        body = None
    outcome = classify(r.status_code, hdrs, body)
    restriction = body['restriction'] if outcome == 'restricted' else None
    new = r.cookies.get('.ROBLOSECURITY')
    return outcome, restriction, new if new and new != cookie else None


def proxy_url(p):
    return f"http://{quote(p['login'], safe='')}:{quote(p['pass'], safe='')}@{p['ip']}:{p['port']}"


def exit_reason(restriction):
    """source 5 — блокировка AccountLock, остальное (1 — бан или предупреждение) — moderated."""
    return 'locked' if (restriction or {}).get('source') == 5 else 'moderated'


def reason_text(outcome, restriction):
    if outcome != 'restricted':
        return outcome
    r = restriction or {}
    kind = 'блокировка AccountLock' if r.get('source') == 5 else 'бан/предупреждение'
    end = f" до {r['endTime']}" if r.get('endTime') else ''
    return f"{kind} (source {r.get('source')}) с {r.get('startTime')}{end}"


def exit_fields(res, now=None):
    """Поля списания в accounts_p2p для restricted (ставить, только если exit_reason ещё нет)."""
    return {'exit_reason': exit_reason(res['restriction']), 'exit_at': now or datetime.utcnow(),
            'exit_note': f"nap gate ({res['action']} -> {res['target']}): {res['reason']}"}


def paused():
    """Почему проверки сейчас на паузе (challenge / rate_limited) или None."""
    return _pause['why'] if time.time() < _pause['until'] else None


def bot_proxy(proxies, proxy_id):
    """Свой прокси бота. Снят (valid False) -> живой прокси той же группы: прод (бэк свопа, `bots`)
    держит у бота только group_id и сам ходит через живой прокси группы (16.11: у 97 из 193 ботов
    /broken свой прокси снят, not_owned). Привязку в accounts_p2p не меняем.
    ponytail: первый живой по _id — одна и та же замена на каждой пробе; балансировать, если упрёмся."""
    px = proxies.find_one({'_id': proxy_id}) if proxy_id else None
    if px and px.get('valid') is False and px.get('group_id'):
        px = proxies.find_one({'group_id': px['group_id'], 'valid': {'$ne': False}}, sort=[('_id', 1)])
        if px:
            logging.info('nap gate: свой прокси снят, проба через живой прокси группы')
    return px if px and px.get('valid') is not False else None


def _probe(acc, proxies, cookie):
    fp = ((acc.get('reg_info') or {}).get('data') or {}).get('fingerprint') or {}
    if not cookie:
        return 'no_cookie', None, None, None
    if not re.search(r'Chrome/\d+', fp.get('user_agent') or ''):
        return 'no_fingerprint', None, None, None
    px = bot_proxy(proxies, acc.get('proxy'))
    if not px:
        return 'no_proxy', None, None, None   # прямого соединения не бывает
    try:
        return (*check(fp, cookie, proxy_url(px)), None)
    except Exception as e:  # текст не пишем: в нём бывает URL прокси с паролем
        name = type(e).__name__
        return ('proxy_error' if 'proxy' in name.lower() or 'CONNECT' in str(e) else 'transport_error'), None, None, name


def gate(accounts, proxies, acc_id, cookie, action, target, send_event, stage=None, also=()):
    """Проверить бота перед выгрузкой: в прод, в сервис, покупателю.

    accounts — коллекция accounts_p2p, proxies — dedicated-servers.group_proxies, acc_id — _id в
    accounts_p2p, cookie — та кука, что уйдёт наружу. action / target — что делаем и куда (в лог).
    send_event(username, event_type, payload) — monitoring.send_event сервиса (Grafana, bot_events).
    stage — в payload, если send_event сервиса сам его не ставит. also — ещё коллекции с той же
    записью (mm2_swap / adopt_swap): туда тоже пишем ротацию куки.

    -> dict: outcome ('clean' — можно отправлять, всё прочее — нельзя), restriction, cookie (отправлять
    эту: при ротации — новая), write_off (restricted: бот в блокировке или бане — списать), reason, cached.
    Не clean -> событие bot_gate_blocked в Grafana (из кэша — без повторного события)."""
    acc = accounts.find_one({'_id': acc_id}, {'username': 1, 'proxy': 1, 'nap_check': 1,
                                              'reg_info.data.fingerprint': 1}) or {}
    now, last = datetime.utcnow(), acc.get('nap_check') or {}
    res = {'cookie': cookie, 'action': action, 'target': target, 'cached': False}
    if last.get('at') and now - last['at'] < RECHECK:
        res.update(outcome=last.get('outcome'), restriction=last.get('restriction'), cached=True)
    elif paused():
        res.update(outcome='paused', restriction=None)   # пауза — состояние процесса, не бота: не пишем
    else:
        outcome, restriction, new, err = _probe(acc, proxies, cookie)
        res.update(outcome=outcome, restriction=restriction)
        if new:  # ротация: принимаем, как браузер; пишем, только если в базе ещё старая
            for c in (accounts, *also):
                c.update_one({'_id': acc_id, 'cookie': cookie}, {'$set': {'cookie': new}})
                c.update_one({'_id': acc_id, 'cookies': cookie}, {'$set': {'cookies.$': new}})
            res['cookie'] = new
            send_event(acc.get('username'), 'bot_cookie_rotated', {
                'action': action, 'target': target, **({'stage': stage} if stage else {})})
        if outcome in PAUSE:
            _pause.update(until=time.time() + PAUSE[outcome], why=outcome)
        accounts.update_one({'_id': acc_id}, {'$set': {'nap_check': {
            'at': now, 'outcome': outcome, 'restriction': restriction, 'action': action, 'target': target}}})
        r = restriction or {}
        logging.info('nap gate: %s %s -> %s (%s)', acc.get('username'), action, outcome, target)
        if outcome != 'clean':
            send_event(acc.get('username'), EVENT, {
                'outcome': outcome, 'reason': reason_text(outcome, restriction),
                'action': action, 'target': target, 'write_off': outcome == 'restricted',
                'source': r.get('source'), 'start_time': r.get('startTime'), 'end_time': r.get('endTime'),
                'moderation_status': r.get('moderationStatus'),
                'restriction': json.dumps(restriction, ensure_ascii=False) if restriction else None,
                'error': err, **({'stage': stage} if stage else {})})
    res['write_off'] = res['outcome'] == 'restricted'
    res['reason'] = reason_text(res['outcome'], res['restriction'])
    return res


def _selfcheck():
    class Col:
        def __init__(self, doc=None):
            self.doc, self.sets = doc, []

        def find_one(self, q, proj=None):
            return self.doc

        def update_one(self, q, upd):
            self.sets.append((q, upd))

    events = []
    send = lambda *a: events.append(a)
    assert classify(200, {}, {'restriction': None}) == 'clean'
    assert classify(200, {}, {'restriction': {'source': 5}}) == 'restricted'
    assert classify(200, {}, {}) == classify(200, {}, None) == 'unexpected'
    assert classify(200, {'rblx-challenge-id': 'x'}, {'restriction': None}) == 'challenge'
    assert classify(403, {'x-csrf-token': 't'}, None) == 'csrf_403' and classify(403, {}, None) == 'forbidden'
    assert classify(401, {}, None) == 'unauthorized' and classify(502, {}, None) == 'http_502'
    assert accept_language(['[en-US] English']) == 'en-US,en;q=0.9' and accept_language(None) == 'en-US,en;q=0.9'
    assert sec_ch_ua(142).count('"Google Chrome";v="142"') == 1
    fp = {'user_agent': 'Mozilla/5.0 (Macintosh) Chrome/142.0.0.0 Safari/537.36'}
    h = headers(fp, 'C')
    assert list(h)[:2] == ['sec-ch-ua-platform', 'user-agent'] and h['sec-ch-ua-platform'] == '"macOS"'
    # нет прокси -> не проверяем, событие в Grafana, кэш на час
    acc = Col({'_id': 1, 'username': 'bot', 'proxy': None, 'reg_info': {'data': {'fingerprint': fp}}})
    res = gate(acc, Col(), 1, 'C', 'to_prod', 'swap MM2', send, stage='s')
    assert res['outcome'] == 'no_proxy' and not res['write_off'] and events[-1][1] == EVENT
    assert events[-1][2]['stage'] == 's' and acc.sets[-1][1]['$set']['nap_check']['outcome'] == 'no_proxy'
    # нет лица -> no_fingerprint (сетевого запроса нет)
    acc.doc = {**acc.doc, 'reg_info': {}, 'proxy': 7}
    assert gate(acc, Col({'_id': 7, 'valid': True}), 1, 'C', 'a', 't', send)['outcome'] == 'no_fingerprint'
    # кэш: блокировка час назад не прошла -> без пробы и без нового события, списать
    lock = {'source': 5, 'startTime': '2026-11-09T19:05:14.782Z', 'endTime': None, 'moderationStatus': 2}
    acc.doc['nap_check'] = {'at': datetime.utcnow() - timedelta(minutes=5), 'outcome': 'restricted', 'restriction': lock}
    n = len(events)
    res = gate(acc, Col(), 1, 'C', 'to_prod', 'swap MM2', send)
    assert res['cached'] and res['write_off'] and len(events) == n
    assert res['reason'] == 'блокировка AccountLock (source 5) с 2026-11-09T19:05:14.782Z'
    ex = exit_fields(res)
    assert ex['exit_reason'] == 'locked' and 'to_prod -> swap MM2' in ex['exit_note']
    assert exit_reason({'source': 1}) == 'moderated'
    # кэш clean -> пропускаем без пробы; старый кэш + пауза -> paused, без записи
    acc.doc['nap_check'] = {'at': datetime.utcnow(), 'outcome': 'clean'}
    assert gate(acc, Col(), 1, 'C', 'a', 't', send)['outcome'] == 'clean'
    acc.doc['nap_check']['at'] -= RECHECK
    _pause.update(until=time.time() + 60, why='challenge')
    k = len(acc.sets)
    assert gate(acc, Col(), 1, 'C', 'a', 't', send)['outcome'] == 'paused' and len(acc.sets) == k
    _pause.update(until=0.0, why=None)
    # свой прокси снят -> первый живой той же группы; в группе живых нет / нет group_id -> None
    class Px:
        def __init__(self, rows):
            self.rows = rows

        def find_one(self, q, proj=None, sort=None):
            hit = [r for r in self.rows if all(r.get(k) == v if not isinstance(v, dict) else r.get('valid') is not False
                                               for k, v in q.items())]
            return min(hit, key=lambda r: r['_id']) if hit else None
    rows = [{'_id': 1, 'group_id': 'g', 'valid': False}, {'_id': 3, 'group_id': 'g', 'valid': True},
            {'_id': 2, 'group_id': 'g'}, {'_id': 4, 'group_id': 'h', 'valid': False}, {'_id': 5, 'valid': False}]
    assert bot_proxy(Px(rows), 1)['_id'] == 2 and bot_proxy(Px(rows), 3)['_id'] == 3
    assert bot_proxy(Px(rows), 4) is None and bot_proxy(Px(rows), 5) is None and bot_proxy(Px(rows), None) is None
    print('nap_gate selfcheck ok')


if __name__ == '__main__':
    _selfcheck()
