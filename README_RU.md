# Cortensor Canary

**Релизный гейт для LLM-эндпоинтов.** Прогоняет ваш собственный набор задач на текущем провайдере и
на кандидате (Cortensor Portal, локальный Cortensor Router, Ollama, vLLM — любой OpenAI-совместимый
эндпоинт) и выдаёт детерминированный вердикт **PASS / WARN / FAIL** с явно названными упавшими кейсами.

> Модель меняют одним сообщением в чате. Никто не проверяет, стала ли замена хуже.
> Canary превращает эту проверку в команду на 30 секунд вместо двухнедельного спора.

![Обзор вердикта](artifacts/demo/report-overview.png)

## Зачем это нужно

Переезд инференса в децентрализованную сеть (Cortensor) или к более дешёвому провайдеру — это
**релиз**, а не правка конфига. Он может молча сломать JSON-вывод, классификацию русских интентов или
латентность. Точечные проверки промптов это не ловят, а оценка «модель судит модель» недостаточно
воспроизводима, чтобы гейтить деплой.

Три правила, на которых построен Canary:

1. **Каждая цифра приходит из вызова инструмента.** Ни одна модель не сообщает метрику, которую не прочитала.
2. **Скоринг детерминированный.** `exact`, `contains`, `regex`, `label`, `json`. Один вход — один вердикт.
3. **Гейт явный.** Падение качества, рост p95 и доля ошибок имеют пороги, которые задаёте вы.

## Быстрый старт

Нужен только Python 3.9+. Ни `pip install`, ни базы данных — для демо ничего ставить не надо.

```bash
git clone https://github.com/9xj89gzrtw-hue/cortensor-openminion-agent.git
cd cortensor-openminion-agent

./canary version          # версия + доступен ли OpenMinion
./canary suites           # встроенные наборы
./canary targets          # настроенные эндпоинты

./canary demo             # полный офлайн-гейт: mock baseline vs деградировавший кандидат
```

`demo` поднимает два локальных mock-эндпоинта, прогоняет набор `smoke` на обоих, применяет гейты
и пишет JSON + Markdown + HTML. Кандидат не проходит, поэтому вердикт — FAIL (сам `demo` возвращает 0,
он демонстрирует гейт; `canary` и `compare` возвращают 1 при FAIL):

```text
                    baseline     candidate       delta
  pass rate           100.0%         83.3%      -16.7pp
  p95                  36.7ms       257.7ms     +602.2%
  errors                   0             0

Cortensor Canary verdict: FAIL
  quality         -16.7pp  (gate -2.0pp)  FAIL
  latency_p95    +602.2%   (gate +25.0%)  FAIL
  errors           +0.0%   (gate +2.0%)  PASS

Regressions: 1 -> instruction-three-colors
Report: artifacts/demo/reports/20261028T113944Z-smoke/report.html
```

Эти цифры — **данные фикстуры из встроенного mock**, а не бенчмарк Cortensor. Они нужны, чтобы пайплайн
тестировался офлайн; профиль `degraded` намеренно отвечает неправильно на один кейс.

## Реальный прогон

```bash
cp .env.example .env
export CORTENSOR_API_KEY=...       # ключ из https://portal.cortensor.network

./canary targets --probe           # один маленький живой запрос на эндпоинт
./canary canary \
  --baseline local \
  --candidate portal \
  --suite support-ru
```

Код выхода: `0` для PASS/WARN, `1` для FAIL — поэтому гейт сразу встраивается в CI:

```yaml
- name: Inference release gate
  run: ./canary canary --baseline current --candidate cortensor --suite support-ru
  env:
    CORTENSOR_API_KEY: ${{ secrets.CORTENSOR_API_KEY }}
```

### Команды

| Команда | Что делает |
|---|---|
| `canary targets [--probe]` | Список эндпоинтов; `--probe` шлёт по одному короткому запросу |
| `canary suites` | Список встроенных наборов и число кейсов |
| `canary run --target X --suite Y` | Один набор на одном эндпоинте, сохраняет артефакт прогона |
| `canary compare A B` | Сравнить два прогона, записать отчёты, exit 1 при FAIL |
| `canary canary --baseline A --candidate B` | Прогнать оба и применить гейты за один шаг |
| `canary demo [--agent]` | Офлайн-демо на mock-эндпоинтах |
| `canary agent "<задача>"` | Провести гейт через агента OpenMinion |
| `canary version` | Версия и наличие OpenMinion |

### Пороги гейта

| Флаг | По умолчанию | Смысл |
|---|---|---|
| `--max-quality-drop` | `2.0` п.п. | Допустимое падение pass rate относительно baseline |
| `--max-latency-increase` | `25.0` % | Рост p95, дающий WARN; вдвое больше — FAIL |
| `--max-error-rate` | `2.0` % | Доля ошибок кандидата, дающая FAIL |

## Встроенные наборы

| Набор | Кейсов | Что проверяет |
|---|---:|---|
| `smoke` | 6 | Арифметика, скидка, извлечение JSON, русская метка интента, следование инструкции, знание |
| `support-ru` | 9 | Русская поддержка: 6 интентов + извлечение номера заказа + JSON с интентом |
| `structured` | 5 | Только JSON: тональность, поля счёта, аргументы инструмента, массивы, флаг эскалации |

Наборы — обычный JSON, свою рабочую нагрузку можно положить в репозиторий:

```json
{
  "name": "my-support",
  "version": "1.0",
  "default_max_tokens": 128,
  "cases": [
    {
      "id": "refund-intent",
      "prompt": "Классифицируй: «Верните деньги за разбитый товар». Ответь меткой: billing, tech, refund, other.",
      "max_tokens": 16,
      "expect": {"type": "label", "value": ["billing", "tech", "refund", "other"]}
    }
  ]
}
```

Типы ожиданий: `exact`, `contains` (`mode: all|any`), `regex`, `label` (не принимает двусмысленный
ответ), `json` (`keys`, `equals`, `unordered`).

## Агент OpenMinion

Гейт — это ещё и агент. `canary agent` запускает OpenMinion `Agent`, инструменты которого оборачивают
детерминированный runner:

```text
list_targets → run_eval(baseline) → run_eval(candidate) → compare_runs → gate_verdict
```

Модель планирует последовательность и объясняет результат, но не может выдумать pass rate: единственные
цифры, которые она видит, приходят из этих вызовов. Инструменты объявлены `READ_ONLY`, поэтому цикл
идёт без интерактивных подтверждений.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install 'openminion>=0.1.14'
openminion setup --provider cortensor-portal --model oss-20b --no-chat   # пишет ~/.openminion/agents.json
export CORTENSOR_API_KEY=...

./canary agent "Может ли Cortensor portal заменить локальный Ollama на наборе support-ru?"
```

Офлайн-демо агента (без ключа и без сети — mock разыгрывает и цикл инструментов):

```bash
CANARY_MOCK_KEY=mock ./canary demo --agent
# ... [agent] inference steps: 9, tool calls: 5, tool errors: 0, tokens: 5189
```

Если `openminion` стоит в другом интерпретаторе, укажите его явно:

```bash
PYTHON=python3.11 ./canary agent "Может ли Cortensor заменить baseline на support-ru?"
```

В OpenMinion есть нативные пресеты `cortensor-portal` и `cortensor-router`, свой адаптер не нужен.

![Детали гейта](artifacts/demo/report-gates.png)

### Доказательства по кейсам

Каждый кейс хранит промпт, ответ, оценку и латентность — регрессия это названный кейс и диф, а не мнение.

![Результаты по кейсам](artifacts/demo/report-cases.png)

## Что появляется в `artifacts/`

```
artifacts/
├── runs/<run-id>.json           # каждый кейс: промпт, ответ, оценка, латентность, токены
└── reports/<timestamp>-<suite>/
    ├── comparison.json          # машиночитаемый вердикт + дельты + регрессии
    ├── report.md                # для комментария в PR
    └── report.html              # самодостаточный, без JS, нормально печатается
```

## Архитектура

```text
suites/*.json ──► runner ──► runs/*.json ──► compare ──► PASS / WARN / FAIL
                     │                          │
              OpenAI-совместимый клиент    report.md + report.html
                     │                          │
        Cortensor / Router / Ollama      инструменты агента OpenMinion
```

- **Только стандартная библиотека** в ядре: `urllib`, `json`, без SDK. Canary должен запускаться в
  голом CI-контейнере.
- **Ретраи** на 408/409/425/429/5xx с экспоненциальным backoff; латентность измеряется по попытке.
- **Учёт токенов** из ответа провайдера, с оценкой `len/4` как запасным вариантом.
- **Секреты не покидают процесс**: в артефакты попадают только имена `api_key_env`.

Подробнее: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Разработка

```bash
make test          # 65 тестов, сеть не нужна
make demo          # офлайн-гейт
make demo-agent    # + цикл агента OpenMinion (нужен установленный openminion)
make lint          # compileall + проверка импортов
```

Без установленного OpenMinion два теста агента пропускаются, остальные проходят.

```
Ran 65 tests in 24.796s
OK
```

## Ограничения, честно

- Встроенные наборы **маленькие и синтетические**. Они доказывают, что пайплайн работает, а не что
  провайдер хорош для вашей нагрузки. Приносите свои кейсы.
- Детерминированный скоринг не оценивает свободный текст. Это осознанно: судья-LLM сделал бы гейт
  невоспроизводимым.
- Латентность с одной машины — не глобальный бенчмарк. Сравнивайте baseline и кандидата с одного хоста,
  в одном прогоне.
- `cortensor-portal` бывает медленным и нестабильным; клиент ретраит, но FAIL из-за таймаутов — это
  реальный сигнал о готовности к релизу, а не баг гейта.

## Лицензия

Apache-2.0. См. [LICENSE](LICENSE).

English version: [README.md](README.md).
