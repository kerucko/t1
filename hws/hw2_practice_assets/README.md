# Артефакты HW2

Собраны в реальном прогоне 1–2 октября 2026 года (UTC+03:00).
[Полный отчёт](../hw2_practice_report.md).

| Артефакт | Назначение |
| --- | --- |
| [versions.json](versions.json) | Вывод команд версий и ID образа |
| [baseline-config.txt](baseline-config.txt) | Фактические DCS-настройки |
| [baseline-verification.json](baseline-verification.json) | Роли портов, повторная загрузка данных, запрет INSERT на реплике |
| [experiment-summary.json](experiment-summary.json) | Вычисленные интервалы между успехами и время восстановления каждого потока |
| [metrics-history.json](metrics-history.json) | Реальная история ролей, timeline, DCS, pg_up и up с шагом 5 с |
| [dashboard-verification.json](dashboard-verification.json) | Проверка 68 PromQL-запросов панелей |
| [final-verification.json](final-verification.json) | Итоговые роли, строки, наличие всех 326 подтверждённых ID и здоровье мониторинга |

Префиксы сценариев: `replica`, `leader`, `etcd-one`, `etcd-quorum`, `haproxy`.
Файлы `*-experiment.json` содержат stop/start, выбранные сервисы и результат
выполнения команд. Для каждого сценария сохранены снимки `before`, `during`, `after`:

- `*-cluster.txt/json` — stdout, время, команда и код завершения `patronictl list`.
- `*-cluster-data.txt/json` — тот же список в машинном формате.
- `*-etcd.txt/json` — фактический `etcdctl endpoint health`.
- `*-containers.txt/json` — `docker compose ps -a`.
- `*-haproxy.txt` — CSV страницы статистики либо ошибка доступа.
- `*-metrics.txt` — ответ HTTP exporter.
- `*-targets.json` — ответ Prometheus Targets; при ошибке доступа используется txt.

Такие же снимки `baseline-*` и `final-*` показывают исходное и итоговое состояния.
Ненулевые коды команд в снимках отказов сохранены намеренно: это наблюдения,
включая невозможность прочитать DCS без кворума.

JPEG-скриншоты без курсора:

- [Исходный кластер](baseline-cluster.jpg), [исходный HAProxy](baseline-haproxy.jpg).
- [Остановленная реплика](replica-during-cluster.jpg).
- [Новый лидер](leader-during-cluster.jpg), [возврат прежнего лидера](leader-after-cluster.jpg).
- [HAProxy без primary при потере кворума](etcd-quorum-haproxy.jpg).
- [Здоровый PostgreSQL во время stop HAProxy](haproxy-during-cluster.jpg).
- [Финальный кластер](final-cluster.jpg), [финальный HAProxy](final-haproxy.jpg).
- [Postgres Overview](grafana-overview.jpg), [PostgreSQL Database](grafana-database.jpg),
  [PostgreSQL Patroni](grafana-patroni.jpg).

CLI-скриншоты сняты с HTML-представления сохранённого фактического stdout;
HTML не исполняет команды и не подменяет результат. Соответствующие исходные
тексты и метаданные лежат рядом. HAProxy и Grafana сняты со страниц браузера.
`prometheus-help.txt` фиксирует проверку доступных CLI-флагов Prometheus.
Логи сборки, запуска, генератора и компонентов (`*.log`) остаются локально
и исключены из Git через `.gitignore`.
