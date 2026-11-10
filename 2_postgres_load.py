"""Carrega o CSV tratado no banco PostgreSQL do ambiente Compose."""

import argparse
import shutil
import subprocess
from pathlib import Path


# ==============================================================================
# CONFIGURACAO
# ==============================================================================

CONTAINER_PADRAO = "etl-marco2-postgres"
USUARIO_BANCO = "etl"
NOME_BANCO = "acidentes"

# A ordem precisa corresponder às colunas do CSV tratado e da tabela temporária.
COLUNAS_CSV = [
    "id",
    "severity",
    "severidade_classificacao",
    "is_grave",
    "qtd_acidentes",
    "start_time",
    "end_time",
    "ano",
    "mes",
    "dia",
    "dia_semana",
    "data",
    "hora",
    "fim_ano",
    "fim_mes",
    "fim_dia",
    "fim_dia_semana",
    "fim_data",
    "fim_hora",
    "duracao_min",
    "city",
    "state",
    "is_capital",
    "visibility_km",
    "weather_condition",
    "crossing",
    "traffic_signal",
]


# ==============================================================================
# SQL DE CRIACAO DO MODELO E DA TABELA TEMPORARIA
# ==============================================================================

SQL_CRIACAO = """
CREATE TABLE IF NOT EXISTS dim_tempo (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    start_time DATE NOT NULL,
    end_time DATE NOT NULL,
    UNIQUE (start_time, end_time)
);

CREATE TABLE IF NOT EXISTS dim_localizacao (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city VARCHAR(200) NOT NULL,
    state VARCHAR(10) NOT NULL,
    is_capital BOOLEAN NOT NULL,
    CONSTRAINT uq_dim_localizacao
        UNIQUE NULLS NOT DISTINCT (city, state, is_capital)
);

CREATE TABLE IF NOT EXISTS dim_clima (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    weather_condition VARCHAR(200) NOT NULL,
    visibility VARCHAR(40),
    CONSTRAINT uq_dim_clima
        UNIQUE NULLS NOT DISTINCT (weather_condition, visibility)
);

CREATE TABLE IF NOT EXISTS dim_infraestrutura (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    traffic_signal BOOLEAN,
    crossing BOOLEAN,
    CONSTRAINT uq_dim_infraestrutura
        UNIQUE NULLS NOT DISTINCT (traffic_signal, crossing)
);

CREATE TABLE IF NOT EXISTS fato_acidente (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tempo_id BIGINT NOT NULL REFERENCES dim_tempo(id),
    localizacao_id BIGINT NOT NULL REFERENCES dim_localizacao(id),
    clima_id BIGINT NOT NULL REFERENCES dim_clima(id),
    infraestrutura_id BIGINT NOT NULL REFERENCES dim_infraestrutura(id),
    severity INTEGER NOT NULL
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM fato_acidente) THEN
        RAISE EXCEPTION
            'fato_acidente already has rows; refusing duplicate load';
    END IF;
END
$$;

CREATE TEMP TABLE etl_stage (
    id TEXT,
    severity TEXT,
    severidade_classificacao TEXT,
    is_grave TEXT,
    qtd_acidentes TEXT,
    start_time TEXT,
    end_time TEXT,
    ano TEXT,
    mes TEXT,
    dia TEXT,
    dia_semana TEXT,
    data TEXT,
    hora TEXT,
    fim_ano TEXT,
    fim_mes TEXT,
    fim_dia TEXT,
    fim_dia_semana TEXT,
    fim_data TEXT,
    fim_hora TEXT,
    duracao_min TEXT,
    city TEXT,
    state TEXT,
    is_capital TEXT,
    visibility_km TEXT,
    weather_condition TEXT,
    crossing TEXT,
    traffic_signal TEXT
);
"""


# ==============================================================================
# SQL DE CARGA NAS DIMENSOES E NA TABELA FATO
# ==============================================================================

SQL_CARGA = """
-- Inclui os valores distintos nas dimensoes. Conflitos existentes sao ignorados.
INSERT INTO dim_tempo (start_time, end_time)
SELECT DISTINCT
    left(start_time, 10)::date,
    left(end_time, 10)::date
FROM etl_stage
ON CONFLICT (start_time, end_time) DO NOTHING;

INSERT INTO dim_localizacao (city, state, is_capital)
SELECT DISTINCT
    COALESCE(NULLIF(city, ''), 'Nao informado'),
    COALESCE(NULLIF(state, ''), 'Nao informado'),
    COALESCE(NULLIF(is_capital, '')::boolean, false)
FROM etl_stage
ON CONFLICT (city, state, is_capital) DO NOTHING;

INSERT INTO dim_clima (weather_condition, visibility)
SELECT DISTINCT
    COALESCE(NULLIF(weather_condition, ''), 'Nao informado'),
    NULLIF(visibility_km, '')
FROM etl_stage
ON CONFLICT (weather_condition, visibility) DO NOTHING;

INSERT INTO dim_infraestrutura (traffic_signal, crossing)
SELECT DISTINCT
    NULLIF(traffic_signal, '')::boolean,
    NULLIF(crossing, '')::boolean
FROM etl_stage
ON CONFLICT (traffic_signal, crossing) DO NOTHING;

-- Liga as dimensoes para gerar uma linha correspondente na tabela fato.
INSERT INTO fato_acidente (
    tempo_id,
    localizacao_id,
    clima_id,
    infraestrutura_id,
    severity
)
SELECT
    tempo.id,
    localizacao.id,
    clima.id,
    infraestrutura.id,
    staging.severity::integer
FROM etl_stage AS staging
JOIN dim_tempo AS tempo
    ON tempo.start_time = left(staging.start_time, 10)::date
    AND tempo.end_time = left(staging.end_time, 10)::date
JOIN dim_localizacao AS localizacao
    ON localizacao.city =
        COALESCE(NULLIF(staging.city, ''), 'Nao informado')
    AND localizacao.state =
        COALESCE(NULLIF(staging.state, ''), 'Nao informado')
    AND localizacao.is_capital =
        COALESCE(NULLIF(staging.is_capital, '')::boolean, false)
JOIN dim_clima AS clima
    ON clima.weather_condition =
        COALESCE(NULLIF(staging.weather_condition, ''), 'Nao informado')
    AND clima.visibility IS NOT DISTINCT FROM
        NULLIF(staging.visibility_km, '')
JOIN dim_infraestrutura AS infraestrutura
    ON infraestrutura.traffic_signal IS NOT DISTINCT FROM
        NULLIF(staging.traffic_signal, '')::boolean
    AND infraestrutura.crossing IS NOT DISTINCT FROM
        NULLIF(staging.crossing, '')::boolean;

-- Mostra a quantidade de registros em cada dimensão e na tabela fato.
SELECT 'dim_tempo' AS tabela, count(*) FROM dim_tempo
UNION ALL
SELECT 'dim_localizacao', count(*) FROM dim_localizacao
UNION ALL
SELECT 'dim_clima', count(*) FROM dim_clima
UNION ALL
SELECT 'dim_infraestrutura', count(*) FROM dim_infraestrutura
UNION ALL
SELECT 'fato_acidente', count(*) FROM fato_acidente
ORDER BY 1;
"""


# ==============================================================================
# EXECUCAO DO CARREGAMENTO
# ==============================================================================

def iniciar_psql(nome_container):
    """Abre o psql no container e retorna o processo com entrada e saida conectadas."""
    comando = [
        "docker",
        "exec",
        "-i",
        nome_container,
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        USUARIO_BANCO,
        "-d",
        NOME_BANCO,
    ]
    return subprocess.Popen(
        comando,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def enviar_csv_para_staging(processo, caminho_csv):
    """Cria a tabela temporaria no psql e copia o CSV diretamente para ela."""
    comando_copy = (
        "\\copy etl_stage ("
        + ",".join(COLUNAS_CSV)
        + ") FROM STDIN WITH (FORMAT csv, HEADER true)\n"
    )

    processo.stdin.write((SQL_CRIACAO + comando_copy).encode())

    # Transfere o arquivo em blocos para evitar carregá-lo inteiro na memória.
    with caminho_csv.open("rb") as arquivo_csv:
        shutil.copyfileobj(arquivo_csv, processo.stdin, length=8 * 1024 * 1024)

    # O marcador encerra o comando \copy; em seguida, executamos carga e análise.
    processo.stdin.write(b"\\.\nANALYZE etl_stage;\n" + SQL_CARGA.encode())
    processo.stdin.close()


def finalizar_psql(processo, caminho_csv):
    """Aguarda o psql, apresenta sua saida e sinaliza falhas."""
    saida = processo.stdout.read().decode(errors="replace")
    erros = processo.stderr.read().decode(errors="replace")
    codigo_retorno = processo.wait()

    if codigo_retorno:
        raise SystemExit(
            f"psql failed ({codigo_retorno}):\n{erros}\n{saida}"
        )

    print(saida)
    print(f"Load completed from {caminho_csv}")


def main():
    """Le argumentos e executa o carregamento do CSV tratado."""
    pasta_script = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        default=pasta_script / "US_Accidents_March23-tratado.csv",
        help="caminho do CSV tratado",
    )
    parser.add_argument(
        "--container",
        default=CONTAINER_PADRAO,
        help="nome do container PostgreSQL",
    )
    argumentos = parser.parse_args()

    if not argumentos.csv.is_file():
        parser.error(f"CSV not found: {argumentos.csv}")

    processo = iniciar_psql(argumentos.container)
    try:
        enviar_csv_para_staging(processo, argumentos.csv)
    except Exception:
        processo.kill()
        raise

    finalizar_psql(processo, argumentos.csv)


if __name__ == "__main__":
    main()
