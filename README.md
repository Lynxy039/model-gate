# model-gate

Локальный OpenAI-compatible proxy для DS4 и OMLX.

## Что делает

- Coder Next (до 8 запросов) и ThinkingCap (до 2) составляют общий `light`-пул: могут быть загружены и работать одновременно. Рабочий лимит контекста для этого пула — 50K на запрос; benchmark показал, что два запроса по 100K почти исчерпывают 128 GiB unified memory.
- Qwen 122B и DS4 — эксклюзивные: ждут окончания всех light-запросов, затем light-модели выгружаются.
- Пока эксклюзивная модель ожидает, новые light-запросы не запускаются, чтобы heavyweight не голодал.
- После exclusive-запроса следующая light-модель выгружает heavyweight перед загрузкой.
- При новом запросе после OMLX TTL proxy сверяет `/v1/models/status` и перезагружает модель, если сервер успел её выгрузить.
- При смене класса выгружается только resident-backend (OMLX); DS4 считается эфемерным и unload hook не требует.
- DS4 остаётся эксклюзивным на время активного запроса, даже если после ответа сам освобождает память.
- `settle_seconds` даёт runtime время освободить временную память перед выгрузкой при переключении классов.
- `unload_settle_seconds` и `load_settle_seconds` добавляют паузу после соответствующей операции.
- Для resident-backend без unload hook переключение запрещается.

Lock действует до полного окончания streaming-ответа.
Зависший inference ограничивается `upstream_timeout`.
Ошибка lifecycle временно блокирует новые запросы
(`lifecycle_failure_cooldown`), чтобы не заспамить admin API.
Если backend настроен с `autostart_command`, proxy запускает его при первом
запросе, ждёт `ready_path` и только потом отправляет inference.

## Запуск

```bash
cp model-gate.example.json model-gate.json
# Отредактировать base_url и admin URL под Mac с моделями
python3.11 -m model_gate --config model-gate.json
```

Прокси пишет в stderr по каждому запросу модель, backend, режим `stream`,
примерный размер контекста в `context_chars`, HTTP-статус и длительность.
`context_chars` — это число символов в `messages` (не точный token count);
для точного подсчёта нужен отдельный tokenizer или `usage` от backend-а.

Если у backend-а включён `discover_models: true`, proxy запрашивает его
`model_list_path` (по умолчанию `/v1/models`) при запуске и обновляет список
при `GET /v1/models`. Недоступный backend не мешает запуску. В example-конфиге
включено обнаружение для портов 8000–8003; backend-ы без `/v1/models` просто
останутся без обнаруженных моделей. Метаданные моделей от backend-а
(`max_model_len`, `supported_parameters` и т.д.) прокидываются в ответе
`/v1/models` как есть — клиенты видят реальный контекст без ручной
конфигурации.

Для backend-а с несколькими локальными моделями используйте
`model_commands`: команда выбирается по имени модели и запускается только при
первом запросе; при переключении proxy останавливает запущенный backend и
стартует нужную команду. Это удобно для эфемерного DS4, где несколько GGUF
делят один порт.

`GET /v1/models` отдаёт объединённый список всех backend-ов. Чтобы каждый
клиентский провайдер видел только свой backend (иначе обнаруженные модели
дублируются во всех провайдерах), используйте per-backend view:
`GET /<backend>/v1/models` и `POST /<backend>/v1/*` (префикс отбрасывается,
роутинг по имени модели как обычно).

В `models.json` Pi направьте каждый провайдер на свой backend, например:

```json
{
  "model-gate-omlx": {
    "baseUrl": "http://127.0.0.1:9000/omlx/v1",
    "api": "openai-completions",
    "apiKey": "none"
  },
  "model-gate-ds4": {
    "baseUrl": "http://127.0.0.1:9000/ds4/v1",
    "api": "openai-completions",
    "apiKey": "none"
  }
}
```

Сервисы `:8001` и `:8003` не должны оставаться доступными в обход proxy.

## TUI

Управление из терминала живёт в `mgpan` (см. ниже); `mgtui` — легаси-минимум
(только стоп backend-ов), оставлен на случай, если панель недоступна.
Admin API (`/admin/status`, `/admin/stats`, `/admin/stop`, `/admin/cancel`)
принимает запросы только с localhost.

## Панель `mgpan`: мониторинг + управление

Gruvbox-панель с логотипом lynxy-dev: живые запросы с ttft и токами/сек
(бокс LIVE), SYSTEM с графиками RAM/GPU/CPU, BACKENDS со списком моделей
(▶ активна, ◆ загружена), статистика с gate, последние запросы.

Панель рисует терминал сама raw-ANSI последовательностями (без ncurses) и
берёт цвета из truecolor-палитры темы lynxy-dev (gruvbox material): рамки
скруглённые ╭╮╰╯ в цвет bg3 (#665c54), как футер Pi; цвета не зависят от
палитры терминала и одинаковы в Ghostty, herdr и по ssh.

```bash
mgrun            # поднимет gate (если не поднят) + откроет панель в этом терминале
mgrun --gpu      # то же + честный GPU% через sudo powermetrics (спросит пароль)
mgrun --compact  # узкий режим (боковая панель, телефон); авто при ширине <80
mgrun --stop     # остановить gate
mgpan            # только панель (gate уже жив)
mgpan --once     # один кадр в stdout без TUI — для скриптов
```

**Узкий режим** (`--compact` или клавиша `c` на лету): лого пропадает, статус
и энергопотребление сжимаются в две строки, вместо четырёхстрочных графиков —
однострочные спарклайны (cpu/gpu/ram/tmp/pwr), боксы бэкендов(stackом),
таблицы MODELS/RECENT — по компактнее (ошибка показана цветом имени модели,
колонка `pre` — скорость префила). Авто-включается при ширине <80 и
пересчитывается при ресайзе терминала (если не запущен с `--compact`).

`mgrun` идемпотентен: живой gate не перезапускается, панель просто
подключается. Gate стартует отдельной сессией (`/tmp/model-gate.pid`,
лог в `model-gate.log`) — закрытие терминала или выход из панели (`q`) его
не убивают; остановить: `Q` в панели (с подтверждением) или `mgrun --stop`.

Клавиши управления (курсор `j`/`k`, опасные — с подтверждением `y`):

| клавиша | действие |
|---|---|
| `s` | стоп backend-а под курсором (освободит RAM; следующий запрос поднимет заново) |
| `w` | прогрев модели — 1-token запрос через gate (пока грузится — видно в LIVE) |
| `r` | рестарт backend-а (стоп + прогрев последней активной модели) |
| `x` | отменить живой запрос (gate рвёт оба соединения, генерация прекращается) |
| `u` | выгрузить загруженную модель oMLX из GPU-пула (её API, ключ берётся из его stats) |
| `o` | рестарт сервера oMLX целиком (`omlx-cli restart`) |
| `Q` | остановить model-gate |
| `PgUp`/`PgDn` | скролл RECENT; `p` — пауза опроса; `c` — узкий режим; `q` — выход |

`POST /admin/cancel {"id": N}` — новый admin-эндпоинт gate: id живого
запроса виден в `LIVE` и `/admin/stats` (`inflight[].id`).

**GPU%% без пароля** (одноразово, по желанию) — разрешить `powermetrics`
в sudoers, тогда `--gpu` не будет спрашивать пароль:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/powermetrics --samplers gpu_power\,cpu_power\,battery\,thermal -i *" | sudo tee /etc/sudoers.d/mgpan
sudo chmod 440 /etc/sudoers.d/mgpan && sudo visudo -c
```

Точные токены стримов gate собирает сам: для `/v1/chat/completions` он
подставляет `stream_options.include_usage` (поддерживается ds4 и omlx) и
забирает usage из последнего SSE-чанка. Отключить: `inject_stream_usage:
false` в конфиге backend-а.

**Prefill/decode t/s ds4**: gate запускает backend-ы с `stderr=PIPE` — вывод
сервера печатается в консоль gate как раньше и параллельно парсится
(`prefill chunk …`, `prompt done …`, `decoding chunk …`). Для omlx prefill
берётся из его usage (`prompt_tokens_per_second`). Если backend запущен
вне gate — его stderr-метрики не видны, только usage.
Статистика живёт в памяти gate и обнуляется при перезапуске;
`GET /admin/stats` отдаёт её целиком.

## Проверки

```bash
python3.11 -m unittest discover -v
```

Конфигурация намеренно не содержит реальных адресов и не запускает сетевые
запросы во время тестов. `upstream_timeout` должен быть достаточно большим для
длинного inference. Endpoint-ы load/unload нужны только resident-backend-ам.
Для эфемерного backend-а укажите `requires_unload: false`. Для DS4 можно
использовать `autostart_command: ["zsh", "-lic", "ds4run"]`: proxy не запускает
второй процесс, если `ready_path` уже отвечает, и остановит запущенный им DS4
при завершении proxy. Значения задержек
нужно подобрать измерением; они являются страховочной паузой, а не подтверждением
состояния. Для строгого режима lifecycle endpoint должен возвращаться только после
завершения операции, а proxy в будущем можно дополнить memory/readiness probe.

### Плагин herdr

`herdr-plugin/` — официальный плагин herdr (манифест `herdr-plugin.toml`):
pane «model-gate» открывает mgpan в split-панели (mgrun поднимет gate при
необходимости), экшен «Open model-gate panel» — то же из меню действий.

```bash
herdr plugin link ~/Projects/model-gate/herdr-plugin   # однократно
herdr plugin pane open --plugin lynxy.model-gate --entrypoint mgpan
```

Узкий pane — панель автоматически переходит в компактный режим.
