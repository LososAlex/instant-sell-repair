# Конфиг repair-worker Instant Sell. Код — копия repair-worker mm2-swap (D:/MD_STASH/mm2-swap/repair-worker),
# отличаются только значения здесь и .env. Правки кода делать в mm2-swap и переносить сюда.
import os

# Секреты — в .env рядом (шаблон .env.example), в git не попадает.
# Загрузчик на stdlib, как в ротаторе: run.bat ставит зависимости только при создании
# venv, новая зависимость (python-dotenv) на уже развёрнутый сервер не доехала бы.
_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_ENV):
    with open(_ENV, encoding='utf-8') as _f:
        for _line in _f:
            _k, _sep, _v = _line.strip().partition('=')
            if _sep and not _k.startswith('#'):
                os.environ.setdefault(_k.strip(), _v.strip().strip('\'"'))

# Прод-API Instant Sell (бэк — копия свопа: /bots/broken, /bots/repair, /bots/{id}/dead).
API_URL = ''  # TODO: https://<хост прода IS>/api/v1/bots
ADMIN_KEY = os.environ.get('SWAPS_ADMIN_KEY', '')  # заголовок x-bots-admin-key

MONGO_URI = os.environ['MONGO_URI']
MONGO_DB = 'mm-inventory'
MONGO_COLLECTION = 'accounts_p2p'  # источник свежих кук (воркеры пишут сюда); тикеты — в mm-inventory.repair_queue

POLL_INTERVAL_SEC = 600  # пауза между опросами (600 = 10 мин)
REQUEST_TIMEOUT_SEC = 30  # таймаут HTTP-запросов
MAX_REPAIR_ATTEMPTS = 3   # подряд неудачных авто-починок (repair != 200) -> тикет

# reason из /broken (поле добавили на бэке). Диспатч в repair_worker.handle_broken по reason.
REASON_BANNED = 'LNKD_RBX_ACCOUNT_IS_BANNED'  # аккаунт забанен -> скип + алерт, не восстанавливаем
REASON_NO_PRVT = 'NO_ACCESS_TO_PRVT_SERVER'   # нет доступа к приват-серверу -> алерт «проверить VIP-ссылку»

COOKIE_REASONS = [None, 'TRADE_INVALID_BOT_COOKIE']  # штатный слом -> починка по кукам (диспатч по state)
CAPTCHA_REASONS = ['TRADE_CAPTCHA_ERROR']            # капча -> тикет на ручную починку
REPAIR_ONLY_REASONS = ['STARTUP_FAILED', 'TRADE_NO_NEEDED_ITEMS_ON_BOT']  # просто дёрнуть /repair с bot_id, без кук

SKIP_REASONS = []  # не поломка бота (бизнес-причина) -> пропуск без действий; сюда добавлять новые

# Списание замодеренных: INVENTORY из /broken с незакрытым тикетом moderated -> DEAD
# (PATCH /bots/{roblox_account_id}/dead, ключ CV). Пустой CV_KEY = не списываем.
CV_KEY = os.environ.get('CV_KEY', '')  # заголовок x-cv-key
MAX_DEAD_PER_TICK = 20  # предохранитель от массового списания: больше за цикл не трогаем

# reason не из KNOWN_REASONS -> считаем новым, разово алертим в general.
KNOWN_REASONS = (COOKIE_REASONS + CAPTCHA_REASONS + REPAIR_ONLY_REASONS
                 + [REASON_BANNED, REASON_NO_PRVT] + SKIP_REASONS)

# Мониторинг -> Grafana (сервис bot_events, как в ФС). Пусто = телеметрия выключена.
MONITORING_URL = ''     # TODO: эндпоинт сервиса bot_events (уточнить у Алексея/Максима)
MONITORING_KEY = os.environ.get('MONITORING_KEY', '')  # x-api-key сервиса
STAGE = 'production'    # для бота незнакомой игры; иначе stage из GAMES
# /broken отдаёт ботов ВСЕХ игр свопа. По game бота (число enum Games) — метка в телеграм и
# stage в Grafana, тот же, что у ротатора этой игры (на сервере mm2: 'mm2-swap-production').
GAMES = {
    # TODO: подтвердить stage; метка с IS — чтобы в общем чате не спутать со свопом
    4: {'label': 'IS MM2', 'stage': 'mm2-instant-sell-production'},
    0: {'label': 'IS Adopt', 'stage': 'adopt-instant-sell-production'},
}

# Телеграм-алерты. Пустой токен = уведомления выключены.
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = -1004451829914  # TODO: свой чат/топики IS, ниже — топики свопа
TELEGRAM_API_BASE = 'http://93.183.95.193:8081'  # свой Bot API-сервер; '' -> https://api.telegram.org
THREAD_IDS = {
    'general': None,  # топик General шлётся БЕЗ message_thread_id (id=1 -> 400 "message thread not found")
    'new_brokens_in_prod': 3,
    'broken_cookie': 5,
    'refresh_updated': 7,
    'broken_refresh': 9,
    'captcha_in_roblox': 11,
}
