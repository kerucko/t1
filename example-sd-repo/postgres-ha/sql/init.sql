\set ON_ERROR_STOP on
BEGIN;

CREATE TABLE IF NOT EXISTS owners (
    id SERIAL PRIMARY KEY,
    owner_name VARCHAR(100) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    event_name VARCHAR(200) NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    owner_name VARCHAR(100) NOT NULL,
    CONSTRAINT fk_events_owners FOREIGN KEY (owner_name)
        REFERENCES owners(owner_name)
        ON DELETE RESTRICT ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_owner_name ON events(owner_name);
CREATE INDEX IF NOT EXISTS idx_owners_name ON owners(owner_name);

COMMENT ON TABLE owners IS 'Справочник владельцев событий';
COMMENT ON COLUMN owners.id IS 'Уникальный идентификатор владельца';
COMMENT ON COLUMN owners.owner_name IS 'Имя владельца (уникальное)';
COMMENT ON TABLE events IS 'Таблица событий';
COMMENT ON COLUMN events.id IS 'Уникальный идентификатор события';
COMMENT ON COLUMN events.event_name IS 'Название события';
COMMENT ON COLUMN events.timestamp IS 'Временная метка события';
COMMENT ON COLUMN events.owner_name IS 'Имя владельца события (ссылка на owners.owner_name)';

INSERT INTO owners (owner_name) VALUES
    ('Иван Петров'), ('Мария Сидорова'), ('Алексей Козлов')
ON CONFLICT (owner_name) DO NOTHING;

INSERT INTO events (event_name, owner_name)
SELECT seed.event_name, seed.owner_name
FROM (VALUES
    ('Встреча с клиентом', 'Иван Петров'),
    ('Презентация проекта', 'Мария Сидорова')
) AS seed(event_name, owner_name)
WHERE NOT EXISTS (
    SELECT 1 FROM events
    WHERE events.event_name = seed.event_name AND events.owner_name = seed.owner_name
);

COMMIT;
