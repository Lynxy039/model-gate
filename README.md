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
