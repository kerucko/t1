"""Write to the primary and read from replicas through separate HAProxy ports."""

import argparse
import logging
import os
import random
import threading
import time

import psycopg2


EVENT_TYPES = ["login", "logout", "click", "purchase", "view_page", "error"]
CONNECT_TIMEOUT = 5
STATEMENT_TIMEOUT_MS = 5000
RETRY_DELAY = 1
LOG = logging.getLogger("traffic")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--write-port", type=int, default=5001)
    parser.add_argument("--read-port", type=int, default=5002)
    return parser.parse_args()


def connection_config(args, role):
    return {
        "host": args.host,
        "port": args.write_port if role == "write" else args.read_port,
        "dbname": os.getenv("PGDATABASE", "postgres"),
        "user": os.getenv("PGUSER", "postgres"),
        "password": os.getenv("PGPASSWORD", "postgres"),
        "sslmode": os.getenv("PGSSLMODE", "disable"),
        "target_session_attrs": "read-write" if role == "write" else "standby",
        "connect_timeout": CONNECT_TIMEOUT,
        "options": "-c statement_timeout={}".format(STATEMENT_TIMEOUT_MS),
        "application_name": "hw2-{}".format(role),
    }


def backend(cursor, expected_recovery):
    cursor.execute("SELECT inet_server_addr()::text, inet_server_port(), pg_is_in_recovery()")
    address, port, recovery = cursor.fetchone()
    if recovery != expected_recovery:
        raise RuntimeError("Unexpected PostgreSQL role: in_recovery={}".format(recovery))
    return "{}:{} in_recovery={}".format(address, port, recovery)


def write_event(conn):
    event = random.choice(EVENT_TYPES)
    with conn:
        with conn.cursor() as cursor:
            source = backend(cursor, expected_recovery=False)
            cursor.execute("SELECT owner_name FROM owners ORDER BY RANDOM() LIMIT 1")
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("owners is empty; load sql/init.sql before starting traffic")
            owner = row[0]
            cursor.execute(
                "INSERT INTO events (event_name, owner_name) VALUES (%s, %s) RETURNING id",
                (event, owner),
            )
            event_id = cursor.fetchone()[0]
    # Only acknowledge after commit. An uncertain commit is never replayed.
    LOG.info("INSERT backend=%s id=%s event=%s owner=%s", source, event_id, event, owner)


def read_events(conn):
    with conn:
        with conn.cursor() as cursor:
            source = backend(cursor, expected_recovery=True)
            cursor.execute("SELECT id, event_name, owner_name FROM events ORDER BY id DESC LIMIT 3")
            rows = cursor.fetchall()
    LOG.info("READ backend=%s last_ids=%s", source, [row[0] for row in rows])


def run_worker(args, role, stop):
    config = connection_config(args, role)
    operation = write_event if role == "write" else read_events
    interval = 1 if role == "write" else 2
    conn = None
    try:
        while not stop.is_set():
            started = time.monotonic()
            delay = interval
            try:
                if conn is None or conn.closed:
                    conn = psycopg2.connect(**config)
                    LOG.info("CONNECTED endpoint=%s:%s", config["host"], config["port"])
                if stop.is_set():
                    break
                operation(conn)
                delay = max(0, interval - (time.monotonic() - started))
            except (psycopg2.Error, RuntimeError) as error:
                LOG.error("FAILED endpoint=%s:%s error=%s", config["host"], config["port"], error)
                if conn is not None:
                    conn.close()
                conn = None
                delay = RETRY_DELAY
            stop.wait(delay)
    finally:
        if conn is not None:
            conn.close()


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    stop = threading.Event()
    workers = [
        threading.Thread(target=run_worker, args=(args, role, stop), name=role)
        for role in ("write", "read")
    ]
    LOG.info("START write=%s:%s read=%s:%s", args.host, args.write_port, args.host, args.read_port)
    for worker in workers:
        worker.start()
    try:
        while any(worker.is_alive() for worker in workers):
            stop.wait(1)
    except KeyboardInterrupt:
        LOG.info("STOP requested; waiting for in-flight operations")
    finally:
        stop.set()
        for worker in workers:
            worker.join()


if __name__ == "__main__":
    main()
