# Benchmark light-пула

[`model_gate.light_pool_benchmark`](model_gate/light_pool_benchmark.py) проверяет,
могут ли Coder Next и ThinkingCap одновременно обработать большие независимые
контексты на OMLX. Он обращается напрямую к `:8001`, чтобы измерить способности
сервера до включения совместного light-пула в proxy.

## Безопасная проверка конфигурации

Она не загружает модели и не запускает inference:

```bash
python3.11 -m model_gate.light_pool_benchmark --contexts 200000
```

На текущем сервере веса моделей составляют примерно 43.9 GiB (Coder) и 23.2 GiB
(ThinkingCap): 67.1 GiB до KV-cache. Memory Guard OMLX ограничен примерно 116.2
GiB, поэтому допустимость 200K определяется пиками KV-cache, а не весами.

## Реальный замер

Начать с лестницы, а не с 200K:

```bash
python3.11 -m model_gate.light_pool_benchmark \
  --contexts 25000,50000,100000,150000,200000 \
  --overhead-tokens 20000 \
  --run
```

`--contexts` — контекст **каждой** из двух моделей. При `200000` и запасе
`20000` benchmark подбирает уникальный user prompt примерно на 180K токенов для
Coder и отдельно для ThinkingCap, затем отправляет оба prefill параллельно.
Уникальные prompt не дают prefix cache скрыть стоимость prefill.

После каждой ступени JSON-отчёт сохраняется в `reports/light-pool-*.json`.
Он содержит OMLX Memory Guard, системный `memory_pressure`, `vm_stat` и RSS
`omlx-server` каждую секунду во время prefill. Если сервер отклонит запрос или
benchmark упадёт, частичный отчёт всё равно сохранится. Не запускайте
одновременно Pi-субагентов: они исказят результат и могут занять память.

Пройденной считается ступень, где оба ответа получены, OMLX не показывает
`hard` pressure, а `free_percent` macOS и `free_bytes` не подходят опасно близко
к нулю. Для production оставьте минимум одну ступень запаса:
если стабильно прошли 200K, рабочий лимит разумно начать со 150K на запрос,
поскольку реальный prompt Pi также содержит system prompt, tools и историю.
