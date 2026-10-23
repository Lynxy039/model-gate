# model-gate

[![Tests](https://github.com/Lynxy039/model-gate/actions/workflows/tests.yml/badge.svg)](https://github.com/Lynxy039/model-gate/actions/workflows/tests.yml)

Локальный OpenAI-compatible proxy: сериализует переключения моделей между
inference-серверами и отдаёт клиенту один алиас `local-auto`.
Панель `mgpan` показывает запросы, статистику, RAM/GPU/CPU и управляет моделями.
Только стандартная библиотека Python, без pip-зависимостей.

## Требования и границы

- **macOS**, Python **3.11+**. Основной профиль — Apple Silicon с 128 GiB unified memory;
  конфигурация не требует именно 128 GiB и не обещает, что любая модель/context поместится.
  GPU/RAM/CPU telemetry и управляемые DS4 процессы ориентированы на macOS.
- Это инструмент для одного пользователя в доверенной локальной сети,
  **не публичный API-сервис**. Аутентификации inference-запросов нет:
  `apiKey: "none"` у клиента — заглушка, а не защита. См. [SECURITY.md](SECURITY.md).
- Гейт допускает запросы только **одной модели** одновременно. Запросы той же
  модели ограничены `max_concurrency`, запросы другой ждут освобождения всех лизов.
  Лиз удерживается до конца ответа, в том числе streaming.
- Гарантия касается моделей, которыми управляет гейт. Уже загруженные независимо
  модели он не обнаруживает и не выгружает автоматически. Не обходите proxy
  прямыми запросами к backend-ам.

## Установка и первый запуск

Только runtime stdlib, установка через `pipx`:

```bash
brew install pipx && pipx ensurepath
pipx install .
model-gate setup       # пошаговый мастер DS4/oMLX, JSON сохраняется в Application Support
model-gate doctor      # схема, бинарники/файлы, порт gate, доступность oMLX, профиль RAM
mgrun --no-gpu         # gate + TUI; отдельные внутренние backend-порты не нужны клиентам
```

Конфиг также можно редактировать вручную; схема — `model-gate.schema.json`.
`model-gate setup --config PATH` создаёт экземпляр с отдельным состоянием. `mgrun`
создаёт базовую конфигурацию мастером при первом запуске.

Личный config/logs/cache/state не хранятся в репозитории. Пример содержит DS4 и
resident oMLX. Backend типы:

- **`ds4`** — GGUF path и параметры модели; gate запускает процесс и выбирает
  случайный свободный loopback порт (или фиксированный override), проверяет readiness.
- **`omlx`** — уже работающий inference-сервис, модели загружаются/выгружаются
  через его admin API; адрес задаётся в одном конфиге.
- **`process`** — собственная managed-команда с `{port}` и списком models.
- **`http`** — работающий OpenAI-compatible сервер. Custom backend-ы подключаются
  одним из этих двух режимов без plugin-framework.

При переключении gate останавливает DS4 до загрузки следующей модели или вызывает
unload hook у resident backend. Если managed endpoint уже занят внешним процессом,
gate отказывается его усыновлять/останавливать. Unload/load hooks должны завершаться
лишь после фактической операции.

Модели из `models` и ключей `model_commands` маршрутизируются по точному имени;
`prefixes` — отдельно, по самому длинному совпадению. Имена моделей между
backend-ами должны быть уникальными. `model_policies` переопределяет
`max_concurrency` конкретной модели; иначе применяется лимит backend-а.

Для resident backend без unload hook переключение запрещается при
`require_unload: true`. Ошибка проверки состояния не считается успешной
выгрузкой. Для управляемого процесса unload hook не нужен: останавливается
процесс. `requires_unload: false` без управляемой команды означает, что
освобождение памяти — ответственность внешнего сервера.

### Один алиас для клиента

```bash
curl http://127.0.0.1:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-auto","messages":[{"role":"user","content":"Hello"}],"max_tokens":64}'
```

Клиенту достаточно `base_url=http://127.0.0.1:9000/v1` и `model=local-auto`.
Цель выбирается клавишей `m` в панели или `POST /admin/select {"model":"..."}`.
`auto_model` — начальный выбор; последний выбор сохраняется в
`~/Library/Application Support/model-gate/instances/<config-id>/selected`.
PID-файл не используется; lock, selected state и cache разделены по пути config.

`GET /v1/models` возвращает **только `local-auto`**, выбранную модель в `selected`
и контекст из metadata backend-а либо `--ctx`. Если контекст неизвестен,
используется fallback 200000 — это не подтверждение возможностей модели.

Для доступа к реальным спискам моделей есть `GET /<backend>/v1/models`.
`POST /<backend>/v1/*` убирает префикс, но по-прежнему маршрутизирует по имени
модели: это представление для discovery, **не разграничение доступа**.

### Discovery, контекст и таймауты

`discover_models: true` включает опрос `model_list_path` (по умолчанию
`/v1/models`) при запуске и при `/admin/status` не чаще раза в 15 секунд.
`GET /<backend>/v1/models` также обновляет свой backend. Корневой `/v1/models`
не запускает discovery. Недоступный сервер не мешает старту.
Метаданные backend-а сохраняются; ранее обнаруженные модели остаются в списке
при временной недоступности сервера.

Гейт не задаёт лимит контекста в токенах: его определяют модель, runtime,
`--ctx` и доступная память. В DS4/DwarfStar рабочее KV занимает RAM; дисковый
KV-кэш сохраняет префиксы между запросами/перезапусками, а SSD streaming весов —
отдельный механизм. Бюджет дискового кэша не заменяет RAM и не гарантирует 1M
контекста. Нет универсального лимита 50K и универсальной формулы памяти
для разных моделей.

- `max_body_bytes` — максимальный размер JSON-тела, по умолчанию 64 MiB.
- `queue_timeout` — ожидание допуска, по умолчанию 600 секунд, затем HTTP 503.
- `upstream_timeout` — таймаут сетевой операции с backend-ом, **не суммарное
  время генерации**: продолжающий отдавать данные стрим может идти дольше.
- `lifecycle_timeout` — таймаут load/unload/status; `startup_timeout` — запуск.
- `lifecycle_failure_cooldown` — пауза после ошибки lifecycle.
- `settle_seconds`, `unload_settle_seconds`, `load_settle_seconds` — дополнительные
  задержки, а не доказательство освобождения памяти.

## Панель и launcher

```bash
./mgrun                 # запустить gate, если нужно, затем открыть панель
./mgrun --no-gpu        # без sudo/powermetrics
./mgrun --compact       # узкий режим; автоматически при ширине <80
./mgrun --stop          # graceful shutdown через gate API
./mgpan                 # подключиться к уже работающему gate
./mgpan --theme nord    # палитра на этот запуск
./mgpan --once --no-gpu # один текстовый кадр, без интерактивного sudo
```

Launcher оставляет gate независимым от терминала; per-config лог находится в
`~/Library/Logs/model-gate/`, состояние и lock — в `~/Library/Application Support/model-gate/`.
Параллельные старты одного config сериализуются; разные конфиги имеют отдельное
состояние. `q` закрывает только панель; `Q` просит gate о graceful shutdown.
`R` просит тот же gate перечитать config и перезапуститься. Управление панелью —
только через API, без `ps`, PID или `omlx-cli`.

Raw ANSI, truecolor, лого MG, без curses. `T` листает `gruvbox`, `catppuccin`
(Mocha), `tokyo-night`, `nord`, `dracula`. Выбор сохраняется в
`${XDG_CONFIG_HOME:-~/.config}/mgpan/theme`. Рамки подсвечиваются при фокусе
терминала (DEC 1004); для tmux требуется `set -g focus-events on`.

Состояние модели — цвет, отдельного значка выбора нет: `sel` — выбранная,
`sel_on` — выбранная и загруженная (жирный), `live` — активный запрос,
`ok` — загруженная невыбранная, `dim` — простой. В Gruvbox это соответственно
янтарный, жирный янтарный, фиолетовый, зелёный и серый. `▸` — только курсор.

| Клавиша | Действие (без подтверждений) |
|---|---|
| `↑`/`↓`, `j`/`k` | Навигация по LIVE, backend-ам и моделям |
| `m` | Выбрать модель и дождаться 1-token warmup |
| `w` | Прогреть текущую цель `local-auto` |
| `s` | Выгрузить/остановить backend; при активных запросах — HTTP 409, сначала `x` |
| `K` | Остановить backend, удалить обычные `*.kv` из его каталога, прогреть цель; при активном inference — 409 |
| `x` | Отменить запрос под курсором |
| `X` | Halt: закрыть очередь, отменить запросы и остановить/unload backend-ы |
| `r` / `R` | Перезагрузить панель / graceful restart gate |
| `Q` / `q` | Остановить gate / выйти из панели |
| `u` | Resume после halt |
| `T`, `c`, `p` | Палитра / компактный режим / заморозить отрисовку (опрос продолжается) |
| `PgUp`/`PgDn` | История RECENT |

GPU% собирается через `sudo powermetrics`; при включённом по умолчанию `--gpu`
может потребоваться пароль. Используйте `--no-gpu` для неинтерактивного запуска.
Температура опционально берётся из установленного `macmon`, энергопотребление —
из `ioreg`. Без этих источников соответствующие метрики отсутствуют.
Не требуется добавлять широкие wildcard-правила NOPASSWD в sudoers.

Статистика — в памяти, до рестарта. Usage собирается из JSON/конца SSE;
для chat streaming автоматически запрашивается `stream_options.include_usage`
(отключение: `inject_stream_usage: false` у backend-а). Живые токены по числу
SSE-событий приблизительные. `ttfb` означает **время до первых байтов**,
не до первого смыслового токена (в JSON API исторический ключ `ttft_s`).
`e2e t/s` в MODELS/RECENT — completion tokens / время запроса после допуска,
включая запуск/prefill, но без ожидания очереди. Скорость decode из stderr DS4 показывается
отдельно у backend-а. Эти значения нельзя напрямую сравнивать.

## HTTP API и безопасность

`POST /v1/*` проксирует JSON-объект с `model`, сохраняет HTTP-ошибки backend-а
и потоковый ответ. Chunked request bodies не поддерживаются; нужен Content-Length.
Административные методы доступны только с loopback-адреса и локальным Host,
browser Origin-запросы отклоняются. Это защита от браузерных запросов/DNS rebinding,
**не аутентификация локальных процессов**.

| Метод | Endpoint | Назначение |
|---|---|---|
| GET | `/healthz`, `/status` | Жив ли gate, допуск/очередь; не гарантия готовности модели |
| GET | `/admin/status`, `/admin/stats`, `/admin/telemetry` | Состояния/очередь, статистика и backend-метрики через gate |
| POST | `/admin/select` | `{"model":"..."}` — сменить цель без warmup |
| POST | `/admin/cancel` | `{"id":N}` — отменить текущий запрос |
| POST | `/admin/stop` | `{"backend":"..."}` — остановить управляемый процесс |
| POST | `/admin/clear-kv` | `{"backend":"..."}` — остановка и чистка KV под блокировкой допуска |
| POST | `/admin/halt`, `/admin/resume` | Закрыть/открыть допуск; halt отменяет очередь и останавливает backend-ы |
| POST | `/admin/shutdown` | Graceful stop; `{"restart":true}` перечитывает config и exec-ит gate

KV-каталоги содержат текст промптов и состояние модели: не публикуйте их и
не удаляйте файлы при работающем сервере. `K` удаляет только обычные `.kv`,
не следует симлинкам файлов и не принимает путь от клиента.

## Проверки

```bash
python3.11 -m unittest discover -v
```

Модели и доступ к внешней сети тестам не нужны. Unit-тесты используют моки;
HTTP integration-тесты поднимают fake backend и proxy на случайных loopback-портах.
GitHub Actions запускает проверки на Linux и macOS.

## Лицензия

MIT — [LICENSE](LICENSE). Автор: [@Lynxy039](https://github.com/Lynxy039).
