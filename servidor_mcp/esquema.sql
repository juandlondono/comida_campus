-- Esquema de datos de Comida Campus (multi-organización).
-- SQL estándar salvo la tabla FTS5 (índice invertido de SQLite). En PostgreSQL o MySQL
-- se reemplaza por tsvector/GIN o FULLTEXT, sin cambiar el resto.

PRAGMA foreign_keys = ON;

-- Cada cliente del producto: una universidad, un centro comercial, una empresa...
CREATE TABLE IF NOT EXISTS organizaciones (
    id                TEXT PRIMARY KEY,
    nombre            TEXT NOT NULL,
    ciudad            TEXT,
    moneda            TEXT NOT NULL DEFAULT 'COP',
    hora_pico_inicio  TEXT,               -- 'HH:MM', para avisos de afluencia
    hora_pico_fin     TEXT,
    fecha_datos       TEXT,               -- fecha de la última recolección
    descripcion       TEXT
);

-- Lugares dentro de la organización (bloques, edificios, pisos...)
CREATE TABLE IF NOT EXISTS zonas (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    organizacion_id  TEXT NOT NULL REFERENCES organizaciones(id),
    nombre           TEXT NOT NULL,
    UNIQUE (organizacion_id, nombre)
);

-- Menús compartidos por varias sedes de una misma marca (p. ej. Cafeterías Latte)
CREATE TABLE IF NOT EXISTS catalogos (
    id               TEXT PRIMARY KEY,
    organizacion_id  TEXT NOT NULL REFERENCES organizaciones(id),
    nombre           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS establecimientos (
    id                       TEXT PRIMARY KEY,
    organizacion_id          TEXT NOT NULL REFERENCES organizaciones(id),
    nombre                   TEXT NOT NULL,
    marca                    TEXT,
    tipo                     TEXT,
    ubicacion                TEXT,
    zona_id                  INTEGER REFERENCES zonas(id),
    referencias              TEXT,       -- JSON: palabras que ubican el lugar ("biblioteca")
    horario                  TEXT,       -- JSON por día o NULL si no está registrado
    afluencia_nivel          TEXT CHECK (afluencia_nivel IN ('baja', 'media', 'alta')),
    afluencia_cuando         TEXT,
    afluencia_nota           TEXT,
    descripcion              TEXT,
    advertencia_vegetariana  TEXT,
    catalogo_id              TEXT REFERENCES catalogos(id),
    activo                   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS productos (
    id                  TEXT PRIMARY KEY,
    establecimiento_id  TEXT REFERENCES establecimientos(id),  -- producto propio de una sede...
    catalogo_id         TEXT REFERENCES catalogos(id),         -- ...o de un catálogo compartido
    nombre              TEXT NOT NULL,
    categoria           TEXT NOT NULL,
    precio_min          INTEGER,          -- pesos enteros; NULL = no registrado
    precio_max          INTEGER,
    vegetariano         TEXT NOT NULL DEFAULT 'por_confirmar'
                        CHECK (vegetariano IN ('si', 'no', 'por_confirmar')),
    nota                TEXT,
    opciones            TEXT,             -- JSON: sabores o variantes
    disponible_hasta    TEXT,             -- 'HH:MM' si solo se vende hasta cierta hora
    disponible          INTEGER NOT NULL DEFAULT 1,
    actualizado         TEXT,             -- fecha/hora de la última edición
    CHECK ((establecimiento_id IS NULL) <> (catalogo_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_productos_est ON productos(establecimiento_id);
CREATE INDEX IF NOT EXISTS idx_productos_cat ON productos(catalogo_id);
CREATE INDEX IF NOT EXISTS idx_productos_precio ON productos(precio_min);
CREATE INDEX IF NOT EXISTS idx_est_org ON establecimientos(organizacion_id);

-- Índice invertido de texto completo: ignora tildes ("sandwich" = "Sándwich")
CREATE VIRTUAL TABLE IF NOT EXISTS productos_fts USING fts5(
    producto_id UNINDEXED,
    texto,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- ---------------------------------------------------------------- control de accesos
-- Usuarios del panel de vendedores. Las contraseñas NUNCA se guardan: solo su hash PBKDF2 + sal.
CREATE TABLE IF NOT EXISTS usuarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario     TEXT NOT NULL UNIQUE,
    nombre      TEXT NOT NULL,
    hash        TEXT NOT NULL,
    sal         TEXT NOT NULL,
    rol         TEXT NOT NULL CHECK (rol IN ('admin', 'vendedor')),
    activo      INTEGER NOT NULL DEFAULT 1,
    creado      TEXT NOT NULL
);

-- Qué establecimientos puede editar cada vendedor (el admin puede todo)
CREATE TABLE IF NOT EXISTS usuario_establecimientos (
    usuario_id          INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    establecimiento_id  TEXT NOT NULL REFERENCES establecimientos(id),
    PRIMARY KEY (usuario_id, establecimiento_id)
);

-- Sesiones abiertas: solo se guarda el hash del token de la cookie
CREATE TABLE IF NOT EXISTS sesiones (
    token_hash  TEXT PRIMARY KEY,
    usuario_id  INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    expira      TEXT NOT NULL
);
