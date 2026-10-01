"""Тикеты починки в mm-inventory.repair_queue — те же правила, что в прод-ФС.

Свопу всегда priority=1. bot_id тикета = mongo _id записи бота (общий для
accounts_p2p и mm2_swap). Лайфцикл как в ФС: queued -> in_progress -> fixed
(ставит внешний Fixer). Воркер не закрывает fixed-тикет своим статусом —
как в ФС, просто до-применяет repair, пока бот в брокенах.
"""
import logging
from datetime import datetime

from pymongo.errors import PyMongoError

PRIORITY = 1
REASON_REFRESH_INVALID = 'refresh_invalid'  # cookies < 2, нужна регенерация кук
REASON_CAPTCHA = 'captcha'
REASON_MODERATED = 'moderated'  # аккаунт замодерен Roblox; тикет ставит ФС-сторона

ACTIVE_STATUSES = ('queued', 'in_progress')


def open_moderated(client, usernames):
    """Ники из списка с незакрытым тикетом moderated. Джойн по username: bot_id тикета —
    _id из accounts_p2p, а не прод-uuid из /broken."""
    try:
        return set(_coll(client).distinct('username', {
            'reason': REASON_MODERATED, 'status': {'$in': list(ACTIVE_STATUSES)},
            'username': {'$in': list(usernames)}}))
    except PyMongoError as e:
        logging.error('moderated lookup failed: %s', e)
        return set()


def _coll(client):
    return client['mm-inventory']['repair_queue']


def last_ticket(client, bot_id):
    """Самый свежий тикет бота (любой reason) с status/reason/created_at, или None."""
    try:
        return _coll(client).find_one(
            {'bot_id': bot_id},
            sort=[('created_at', -1)],
            projection={'status': 1, 'reason': 1, 'created_at': 1})
    except PyMongoError as e:
        logging.error('last-ticket lookup failed %s: %s', bot_id, e)
        return None


def create_ticket(client, bot_id, username, reason):
    """queued-тикет с priority=1, если активного нет. True если создан."""
    try:
        if _coll(client).find_one({'bot_id': bot_id, 'status': {'$in': list(ACTIVE_STATUSES)}},
                                  projection={'_id': 1}):
            return False
        # последний тикет этой причины уже failed (фиксер не смог) -> не плодим новый.
        # разблокировка: удалить/переоткрыть failed-тикет.
        last = _coll(client).find_one({'bot_id': bot_id, 'reason': reason},
                                      sort=[('created_at', -1)], projection={'status': 1})
        if last is not None and last.get('status') == 'failed':
            logging.info("last '%s' ticket failed for %s -> skip new ticket", reason, username)
            return False
        _coll(client).insert_one({
            'bot_id': bot_id,
            'username': username,
            'priority': PRIORITY,
            'reason': reason,
            'status': 'queued',
            'attempts': 0,
            'created_at': datetime.utcnow(),
            'claimed_at': None,
            'resolved_at': None,
            'notes': None,
        })
        logging.info('ticket created: %s reason=%s', username, reason)
        return True
    except PyMongoError as e:
        logging.error('create ticket failed %s: %s', username, e)
        return False


def resolve_moderated(client, username):
    """Бот списан в DEAD -> закрываем его незакрытые тикеты moderated: чинить больше нечего."""
    try:
        _coll(client).update_many(
            {'reason': REASON_MODERATED, 'status': {'$in': list(ACTIVE_STATUSES)},
             'username': username},
            {'$set': {'status': 'resolved', 'resolved_at': datetime.utcnow(),
                      'notes': 'bot DEAD in swap prod (moderated INVENTORY) by swap repair-worker'}})
    except PyMongoError as e:
        logging.error('resolve moderated failed %s: %s', username, e)


def resolve_ticket(client, ticket_id, note='resolved by swap repair-worker'):
    """Терминальный статус после применения repair. Для капчи: чтобы новая CAPTCHA
    видела, что прошлый тикет уже залит в прод, и завела новый тикет."""
    try:
        _coll(client).update_one(
            {'_id': ticket_id},
            {'$set': {'status': 'resolved', 'resolved_at': datetime.utcnow(), 'notes': note}})
    except PyMongoError as e:
        logging.error('resolve ticket failed %s: %s', ticket_id, e)
