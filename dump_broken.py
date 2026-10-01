"""Дамп ответа GET /bots/broken — посмотреть поля (в т.ч. новый reason) и их значения.

Запуск: python dump_broken.py
"""
import json
from collections import Counter

import requests

import config

HEADERS = {'x-bots-admin-key': config.ADMIN_KEY}


def main():
    r = requests.get(config.API_URL + '/broken', headers=HEADERS, timeout=config.REQUEST_TIMEOUT_SEC)
    print('HTTP', r.status_code)
    if r.status_code != 200:
        print(r.text[:1000])
        return
    bots = r.json().get('data') or []
    print(f'ботов: {len(bots)}\n')

    if bots:
        print('--- поля первого элемента ---')
        print(json.dumps(bots[0], ensure_ascii=False, indent=2))

    # что за значения в state / reason (то, ради чего дамп)
    print('\n--- распределение state / reason ---')
    combos = Counter((b.get('state'), b.get('reason')) for b in bots)
    for (state, reason), n in combos.most_common():
        print(f'  state={state!r:15} reason={reason!r:25} -> {n}')

    # все встреченные ключи (вдруг прогеры добавили не только reason)
    keys = set().union(*(b.keys() for b in bots)) if bots else set()
    print('\n--- все ключи элемента ---')
    print(' ', sorted(keys))


if __name__ == '__main__':
    main()
