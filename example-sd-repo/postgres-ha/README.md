# Практика PostgreSQL HA

Стенд для HW2: три узла PostgreSQL с Patroni, три узла etcd, HAProxy,
Prometheus, PostgreSQL exporter и Grafana. Отчёт находится в
[hws/hw2_practice_report.md](../../hws/hw2_practice_report.md).

Стенд проверен 1–2 октября 2026 года на ARM64: сборка, повторная загрузка SQL,
чтение/запись, пять сценариев отказа и три дашборда. Фактические результаты,
времена и скриншоты собраны в отчёте. Worktree используют общий Docker daemon;
фиксированные container_name и порты требуют согласования параллельных запусков.

## Адреса и маршрутизация

| Назначение | Адрес на хосте | Адрес внутри Compose |
| --- | --- | --- |
| Запись на primary | `localhost:5001` | `haproxy:5000` |
| Чтение с реплик | `localhost:5002` | `haproxy:5001` |
| Статистика HAProxy | <http://localhost:7001/> | `haproxy:7000` |
| Prometheus | <http://localhost:9090/> | `prometheus:9090` |
| Grafana | <http://localhost:3000/> | `grafana:3000` |

Для выполненного прогона стандартные порты мониторинга были заняты HW1,
поэтому использованы Grafana **3001**, Prometheus **9091**, exporter **9188**.
Чтобы повторить эти адреса до запуска:

```bash
cp .env.example .env
```

Без `.env` применяются стандартные порты из таблицы. Primary/replica и HAProxy
stats используют 5001/5002/7001 в обоих вариантах.

HAProxy проверяет `/primary` и `/replica` REST API Patroni на порту `8008`.
При удалении backend из доступных закрывает его клиентские сессии. Генератор
после ошибки создаёт новое соединение через тот же публичный порт.
Балансировка происходит при подключении: постоянная read-сессия может
обслуживаться одной репликой, пока соединение не оборвётся.

## Подготовка и запуск

Нужны работающий Docker daemon, Docker Compose и Python 3.9 или новее.
Все команды выполняются из `example-sd-repo/postgres-ha`.

```bash
docker build -t patroni ./patroni-master
docker compose up -d
docker compose ps
docker compose exec -T patroni1 patronictl list
docker compose exec -T patroni1 patronictl show-config
docker compose exec -T patroni1 etcdctl endpoint health
```

Дождитесь одного `Leader` в состоянии `running`, двух `Replica` в состоянии
`streaming` и трёх здоровых endpoint etcd. Имена ролей определяются динамически:
`patroni1` не обязательно станет лидером. Если кластер ещё не готов, повторите
проверки и посмотрите `docker compose logs --tail=100 patroni1 patroni2 patroni3`.
Нельзя начинать эксперименты, пока исходное состояние не восстановлено.

Образ использует PostgreSQL 17 по умолчанию. PostgreSQL exporter закреплён на
`v0.17.0`, для которого PostgreSQL 17 входит в
[список CI-проверяемых версий](https://github.com/prometheus-community/postgres_exporter/blob/v0.17.0/README.md).
В дашбордах оставлены счётчики bgwriter, доступные в PostgreSQL 17; старые
checkpoint/backend buffer counters не запрашиваются.

### Загрузка данных

Явно загрузите схему через primary после готовности кластера:

```bash
docker compose exec -T -e PGPASSWORD=postgres patroni1 \
  psql -h haproxy -p 5000 -U postgres -d postgres < sql/init.sql
```

`init.sql` содержит таблицы, связи, индексы, комментарии и исходные данные из
задания. Повторное последовательное выполнение сохраняет существующие данные
и не дублирует исходные события. Скрипт не мигрирует ранее созданную несовместимую
схему; ошибки останавливают загрузку, транзакция откатывается.

### Проверка подключения

Если установлен локальный `psql`:

```bash
PGPASSWORD=postgres psql -h localhost -p 5001 -U postgres -d postgres \
  -c 'SELECT inet_server_addr(), inet_server_port(), pg_is_in_recovery();'
PGPASSWORD=postgres psql -h localhost -p 5002 -U postgres -d postgres \
  -c 'SELECT inet_server_addr(), inet_server_port(), pg_is_in_recovery();'
```

Ожидается `false` для `5001` и `true` для `5002`. При отсутствии локального
клиента используйте `docker compose exec -T -e PGPASSWORD=postgres patroni1 psql`
с `-h haproxy` и внутренними портами `5000` и `5001` соответственно.

Проверка запрета записи на реплику должна завершиться ошибкой read-only:

```bash
PGPASSWORD=postgres psql -v ON_ERROR_STOP=1 -h localhost -p 5002 -U postgres -d postgres \
  -c "BEGIN; INSERT INTO events (event_name, owner_name) VALUES ('read-only probe', 'Иван Петров'); ROLLBACK;"
```

При ошибочной маршрутизации успешная вставка откатится благодаря `ROLLBACK`.
В DBeaver создайте два подключения с портами `5001` и `5002`; база, пользователь
и пароль — `postgres`, SSL выключен. Сопоставьте выданный адрес backend с `Host`
из `patronictl list`.

### Генератор

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade 'pip<26'
.venv/bin/python -m pip install -r requirements.txt
mkdir -p ../../hws/hw2_practice_assets
.venv/bin/python -u traffic-generator.py 2>&1 \
  | tee ../../hws/hw2_practice_assets/traffic.log
```

Генератор пишет раз в секунду через `5001` и читает раз в две секунды через
`5002` в независимых потоках. Параметры `--host`, `--write-port`, `--read-port`
меняют адреса; `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE`
задают параметры подключения. По умолчанию используются значения таблицы выше
и учётные данные `postgres/postgres`.

Логи включают время с часовым поясом, `read`/`write`, адрес фактического backend,
роль и ID событий. Успешный `INSERT` логируется после commit. Ошибка подключения
или SQL закрывает только соответствующее соединение; новая попытка начинается
через секунду. Таймауты подключения и каждого SQL-запроса — 5 секунд.
Чтение не держит долгой транзакции и может временно отставать от записи из-за
асинхронной репликации.

При обрыве связи во время commit результат вставки может быть неизвестен.
Генератор не повторяет ту же вставку, а на следующем шаге создаёт новое событие.
ID могут иметь пропуски даже без потери данных, поэтому пропуск ID сам по себе
не доказывает потерю подтверждённой записи. Завершение — Ctrl+C; текущие операции
должны закончиться или получить ошибку, затем соединения закрываются.

## Мониторинг

Grafana автоматически подключает Prometheus с UID `E_pt0RZDk` и загружает три
дашборда в папку `PostgreSQL HA`. Для нового хранилища Grafana вход — `admin/admin`;
если пароль уже изменяли, используйте действующий. Provisioning управляет
дашбордами из JSON, поэтому постоянные изменения нужно делать в файлах.

Prometheus опрашивает каждый Patroni и один PostgreSQL exporter с интервалом
5 секунд. В разделе Targets проверьте четыре доступных target. Exporter
подключён к primary через `haproxy:5000`: его SQL-метрики относятся к серверу,
который обслуживает это соединение, и могут переключиться после failover.
Метрики PostgreSQL exporter не описывают сразу все три PostgreSQL-ноды.
CPU, память и файловые дескрипторы процесса на втором дашборде относятся к
самому exporter, а не к PostgreSQL.

Для экспериментов полезны:

- `up` — доступность HTTP target; `pg_up` — доступность БД для exporter.
- `patroni_primary`, `patroni_replica`, `patroni_postgres_running` — роли и состояние.
- `patroni_postgres_timeline` — timeline после смены лидера.
- `patroni_dcs_last_seen` — время последнего контакта с DCS.
- WAL positions primary и реплик — продвижение репликации.

Счётчики транзакций отражают также служебные запросы и мониторинг. Для скорости
требуется несколько samples; сразу после запуска rate-панели могут быть пустыми.
Не подменяйте отсутствие метрики нулевым значением.

## Эксперименты с отказами

Проводите сценарии последовательно в отдельном терминале, оставляя генератор
работать. Перед каждым сценарием определите текущего лидера, сохраните состояние
и убедитесь в восстановлении одного лидера, двух streaming-реплик и трёх etcd.
Выбирайте `patroni1`, `patroni2` или `patroni3` по фактической роли.

```bash
date "+%Y-%m-%dT%H:%M:%S%z"
docker compose exec -T patroni1 patronictl list
docker compose exec -T patroni1 patronictl show-config
docker compose exec -T patroni1 etcdctl endpoint health
```

Если `patroni1` остановлен, выполняйте диагностические команды из любой живой
Patroni-ноды. При потере кворума `patronictl` может не получить актуальный список;
сохраняйте ошибку и используйте логи, метрики и запросы SQL.

| Сценарий | Действие | Что фиксировать |
| --- | --- | --- |
| Реплика | `docker compose stop <имя-реплики>`, затем `start` | Продолжение записи, переподключение read-сессии при попадании на эту реплику, её возвращение |
| Лидер | `docker compose stop <имя-лидера>`, затем `start` | Новый лидер, ошибки/пауза записи, timeline, прежний лидер после возвращения |
| Один etcd | `docker compose stop etcd1`, затем `start etcd1` | Здоровье оставшихся endpoint, роли, чтение и запись |
| Два etcd | `docker compose stop etcd1 etcd2`, затем `start etcd1 etcd2` | Потеря кворума, состояние БД и маршрутов, время восстановления |
| HAProxy | `docker compose stop haproxy`, затем `start haproxy` | Ошибки обоих потоков при работающих БД, восстановление клиентского доступа |

`<имя-реплики>` и `<имя-лидера>` — placeholders; замените их перед выполнением.
Фиксируйте время непосредственно перед stop/start. Остановку лидера сначала
проводите штатным `stop`. Для отдельного сравнения аварийного отказа после полного
восстановления можно повторить сценарий с `docker compose kill -s SIGKILL <имя-лидера>`
и последующим `start`; не смешивайте результаты двух способов остановки.

Текущая исходная конфигурация: `ttl=30`, `loop_wait=10`, `retry_timeout=10`,
асинхронная репликация; `failsafe_mode` не включён. Сверьте значения через
`show-config`, поскольку bootstrap-настройки не заменяют существующую конфигурацию
в DCS. При потере двух etcd наблюдайте не менее 60 секунд. Ожидаемая гипотеза —
лидер перестанет обслуживать запись при невозможности поддерживать лидерский
ключ; фактическое поведение чтения и HAProxy нужно измерить.

Измеряйте отдельно время от stop до первой ошибки, паузу между успешными
операциями и время от start до первого успешного чтения/записи. Восстановление
после отказа лидера может произойти ещё до возврата прежней ноды.
После каждого сценария сохраните `patronictl list`, логи затронутых контейнеров,
статистику HAProxy и скриншот Grafana с тем же временным диапазоном.

В конце верните все остановленные контейнеры командой `docker compose start`,
подтвердите здоровый кластер и работу обоих потоков. Не используйте `down -v`,
`rm` или `up --force-recreate` для этих экспериментов: данные PostgreSQL и etcd
в текущем стенде живут в файловой системе контейнеров и не защищены named volumes.

## Проверенное состояние

В выполненном прогоне использованы PostgreSQL 17.11, Patroni 4.1.0,
etcd 3.3.13, HAProxy 3.0.11, Prometheus 3.15.0 и Grafana 9.0.0.
После всех отказов восстановлены три Patroni, три etcd и HAProxy;
генератор завершён через Ctrl+C. Все 326 подтверждённых вставок найдены
на primary и реплике, все четыре target доступны, 68 запросов панелей
возвращают данные. Подробности и ограничения измерений — в отчёте.
