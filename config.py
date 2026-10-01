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
API_URL = 'https://neural-product-isell-87ca8e26b5bd.herokuapp.com/api/v1/bots'
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
MONITORING_URL = 'http://37.27.99.126:8000'   # bot_events, тот же сервис, что у свопа
MONITORING_KEY = os.environ.get('MONITORING_KEY', '')  # x-api-key сервиса
STAGE = 'production'    # для бота незнакомой игры; иначе stage из GAMES
# /broken отдаёт ботов ВСЕХ игр свопа. По game бота (число enum Games) — метка в телеграм и
# stage в Grafana, тот же, что у ротатора этой игры (на сервере mm2: 'mm2-swap-production').
GAMES = {
    # метка с IS — чтобы не спутать со свопом
    4: {'label': 'IS MM2', 'stage': 'mm2-instant-sell-production'},
    0: {'label': 'IS Adopt', 'stage': 'adopt-instant-sell-production'},
}

# Телеграм-алерты. Пустой токен = уведомления выключены.
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = -1004296860031  # «Instant-sell_Repair», бот @NotifyInstantSellBot
TELEGRAM_API_BASE = ''  # облачный api.telegram.org: со стенда он доступен напрямую
BROKEN_TITLE = 'Новые брокен-боты в Instant Sell'   # заголовок списка новых брокенов
THREAD_IDS = {
    'general': None,  # топик General шлётся БЕЗ message_thread_id (id=1 -> 400 "message thread not found")
    'new_brokens_in_prod': 4,  # Новые брокены
    'broken_cookie': 5,        # Нет в базе
    'refresh_updated': 6,      # Куки обновлены
    'broken_refresh': 7,       # Сломан рефреш
    'captcha_in_roblox': 8,    # Капча
}
