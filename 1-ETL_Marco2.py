"""Prepara o CSV do US Accidents conforme o Marco 2, sem alterar a entrada.

Python 3.10+, somente biblioteca padrão.
Uso: python etl_marco2.py --entrada arquivo.csv --saida resultado.csv
CSV: UTF-8, separador vírgula, ponto decimal, datas ISO e booleanos true/false.
Campos vazios representam NULL na futura importação para PostgreSQL.
O resultado é uma tabela de preparação; a carga do modelo estrela é posterior.
"""

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing, ExitStack
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


# ==============================================================================
# 1. CONSTANTES E TABELAS DE REFERÊNCIA
# ==============================================================================

# 48 estados contíguos. DC não é capital estadual; AK/HI estão fora do escopo.
# Referência: https://www.leg.mn.gov/docs/2013/other/130552.pdf
CAPITAIS = {
    'AL': 'Montgomery',     'AZ': 'Phoenix',        'AR': 'Little Rock',
    'CA': 'Sacramento',     'CO': 'Denver',         'CT': 'Hartford',
    'DE': 'Dover',          'FL': 'Tallahassee',    'GA': 'Atlanta',
    'ID': 'Boise',          'IL': 'Springfield',    'IN': 'Indianapolis',
    'IA': 'Des Moines',     'KS': 'Topeka',         'KY': 'Frankfort',
    'LA': 'Baton Rouge',    'ME': 'Augusta',        'MD': 'Annapolis',
    'MA': 'Boston',         'MI': 'Lansing',        'MN': 'Saint Paul',
    'MS': 'Jackson',        'MO': 'Jefferson City', 'MT': 'Helena',
    'NE': 'Lincoln',        'NV': 'Carson City',    'NH': 'Concord',
    'NJ': 'Trenton',        'NM': 'Santa Fe',       'NY': 'Albany',
    'NC': 'Raleigh',        'ND': 'Bismarck',       'OH': 'Columbus',
    'OK': 'Oklahoma City',  'OR': 'Salem',          'PA': 'Harrisburg',
    'RI': 'Providence',     'SC': 'Columbia',       'SD': 'Pierre',
    'TN': 'Nashville',      'TX': 'Austin',         'UT': 'Salt Lake City',
    'VT': 'Montpelier',     'VA': 'Richmond',       'WA': 'Olympia',
    'WV': 'Charleston',     'WI': 'Madison',        'WY': 'Cheyenne',
}

ORIGINAIS = [
    'ID', 'Severity', 'Start_Time', 'End_Time', 'City', 'State',
    'Visibility(mi)', 'Weather_Condition', 'Crossing', 'Traffic_Signal',
]

TEMPORAIS = ['ano', 'mes', 'dia', 'dia_semana', 'data', 'hora']

COLUNAS = (
    ['ID', 'Severity', 'severidade_classificacao', 'is_grave', 'qtd_acidentes', 'Start_Time', 'End_Time']
    + TEMPORAIS
    + ['fim_' + c for c in TEMPORAIS]
    + ['duracao_min', 'City', 'State', 'is_capital', 'Visibility(km)', 'Weather_Condition', 'Crossing', 'Traffic_Signal']
)

DIAS = [
    'Segunda-feira', 'Terça-feira', 'Quarta-feira',
    'Quinta-feira', 'Sexta-feira', 'Sábado', 'Domingo',
]

CLASSIFICACAO_SEVERIDADE = {
    1: 'Leve',
    2: 'Moderada',
    3: 'Grave',
    4: 'Grave',
}

TRADUCOES_CLIMA = {
    'fair': 'Tempo bom',
    'clear': 'Céu limpo',
    'partly cloudy': 'Parcialmente nublado',
    'mostly cloudy': 'Predominantemente nublado',
    'cloudy': 'Nublado',
    'overcast': 'Encoberto',
    'thunder': 'Trovões',
    't-storm': 'Tempestade',
    't-storms': 'Tempestade',
    'thunderstorm': 'Tempestade',
    'thunderstorms': 'Tempestade',
    'thunderstorms and rain': 'Tempestade com chuva',
    'rain': 'Chuva',
    'drizzle': 'Garoa',
    'light freezing rain': 'Chuva congelante leve',
    'freezing rain': 'Chuva congelante',
    'freezing drizzle': 'Garoa congelante',
    'fog': 'Nevoeiro',
    'shallow fog': 'Nevoeiro raso',
    'patches of fog': 'Bancos de nevoeiro',
    'haze': 'Névoa seca',
    'mist': 'Névoa úmida',
    'smoke': 'Fumaça',
    'sleet': 'Chuva de gelo',
    'ice pellets': 'Grãos de gelo',
    'hail': 'Granizo',
    'small hail': 'Granizo pequeno',
    'wintry mix': 'Precipitação invernal mista',
    'blowing dust': 'Poeira suspensa pelo vento',
    'widespread dust': 'Poeira generalizada',
    'sand': 'Areia',
    'blowing sand': 'Areia suspensa pelo vento',
    'dust whirls': 'Redemoinhos de poeira',
    'sand / dust whirls nearby': 'Redemoinhos de areia ou poeira próximos',
    'squalls': 'Rajadas de vento',
    'tornado': 'Tornado',
    'volcanic ash': 'Cinzas vulcânicas',
    'funnel cloud': 'Nuvem funil',
    'blowing spray': 'Borrifos de água pelo vento',
}

AUSENTES = {'', 'nan', 'null', 'none', 'n/a', 'na'}
KM_POR_MILHA = Decimal('1.609344')
MICROSSEGUNDOS_POR_MINUTO = Decimal(60_000_000)


# ==============================================================================
# 2. FUNÇÕES DE LIMPEZA E CONVERSÃO DE CAMPOS
# ==============================================================================

def ausente(valor):
    """Verifica se o texto representa um valor vazio ou nulo."""
    return valor.strip().casefold() in AUSENTES


def normalizar_cidade(valor):
    """Padroniza o nome da cidade apenas para comparação (ex.: 'St. Paul' -> 'saint paul')."""
    texto_limpo = ' '.join(valor.casefold().replace('.', '').split())
    return texto_limpo.replace('st ', 'saint ')


def clima_pt(valor):
    """Traduz e agrupa a condição climática; rejeita categorias desconhecidas."""
    if ausente(valor):
        return 'Não informado'

    texto = valor.strip().casefold()

    # Regra prioritária: qualquer menção a neve (mesmo mista com chuva) vira 'Neve'.
    if 'snow' in texto:
        return 'Neve'

    # Remove sufixos de vento e prefixos de intensidade para manter apenas o fenômeno principal.
    texto = re.sub(r'\s*/\s*(windy|wind)\s*$', '', texto)
    texto = re.sub(r'^(light|heavy)\s+', '', texto)
    texto = texto.removeprefix('conditions of ')

    # Trata indicadores de proximidade ('in the vicinity' ou 'nearby').
    proximidade = ''
    if texto.endswith(' in the vicinity'):
        texto = texto.removesuffix(' in the vicinity')
        proximidade = ' nas proximidades'
    elif texto.endswith(' nearby') and texto not in TRADUCOES_CLIMA:
        texto = texto.removesuffix(' nearby')
        proximidade = ' nas proximidades'

    if texto not in TRADUCOES_CLIMA:
        raise ValueError('Condição climática sem tradução definida: ' + valor)

    return TRADUCOES_CLIMA[texto] + proximidade


def booleano(valor):
    """Converte representações booleanas ('true', 'false', '1', '0') para formato padrão."""
    if ausente(valor):
        return ''

    tabela = {'true': 'true', 'false': 'false', '1': 'true', '0': 'false'}
    try:
        return tabela[valor.strip().casefold()]
    except KeyError:
        raise ValueError('Booleano inválido: ' + valor) from None


def data_hora(valor):
    """Converte string ISO para datetime, truncando para precisão de microssegundos."""
    if ausente(valor):
        return None

    # PostgreSQL TIMESTAMP suporta até 6 casas decimais (microssegundos).
    texto = re.sub(r'(\.\d{6})\d+', r'\1', valor.strip())
    try:
        resultado = datetime.fromisoformat(texto)
    except ValueError:
        raise ValueError('Data/hora inválida: ' + valor) from None

    if resultado.tzinfo is not None:
        raise ValueError('Timestamp com fuso: requer decisão explícita de conversão')

    return resultado


def extrair_tempo(instante, prefixo=''):
    """Decompõe um datetime em colunas de ano, mês, dia, dia da semana, data e hora."""
    if instante is None:
        valores = ['', '', '', '', '', '']
    else:
        valores = [
            instante.year,
            instante.month,
            instante.day,
            DIAS[instante.weekday()],
            instante.date().isoformat(),
            instante.hour,
        ]
    return {f'{prefixo}{coluna}': valor for coluna, valor in zip(TEMPORAIS, valores)}


def calcular_duracao_minutos(inicio, fim):
    """Calcula a diferença em minutos entre dois timestamps usando precisão Decimal."""
    if inicio is None or fim is None:
        return ''

    delta = fim - inicio
    if delta.total_seconds() < 0:
        raise ValueError('End_Time anterior a Start_Time')

    total_microssegundos = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return format(Decimal(total_microssegundos) / MICROSSEGUNDOS_POR_MINUTO, 'f')


def converter_visibilidade_km(valor_milhas):
    """Converte a visibilidade de milhas para quilômetros."""
    if ausente(valor_milhas):
        return ''

    try:
        milhas = Decimal(valor_milhas.strip())
    except InvalidOperation:
        raise ValueError('Visibilidade inválida') from None

    if not milhas.is_finite() or milhas < 0:
        raise ValueError('Visibilidade deve ser finita e não negativa')

    return format(milhas * KM_POR_MILHA, 'f')


# ==============================================================================
# 3. TRANSFORMAÇÃO DE CADA REGISTRO (LINHA DO CSV)
# ==============================================================================

def transformar(linha):
    """Valida e transforma uma linha bruta do CSV no dicionário do formato final."""
    # 1. Validação do ID
    identificador = linha['ID'].strip()
    if ausente(identificador):
        raise ValueError('ID ausente')

    # 2. Validação do Estado (48 estados contíguos + DC)
    estado = linha['State'].strip().upper()
    if estado not in CAPITAIS and estado != 'DC':
        raise ValueError('Estado ausente, inválido ou fora do escopo: ' + estado)

    # 3. Validação e classificação da Severidade (1 a 4)
    try:
        severidade = int(linha['Severity'])
    except ValueError:
        raise ValueError('Severity ausente ou inválida') from None

    if severidade not in CLASSIFICACAO_SEVERIDADE:
        raise ValueError('Severity fora do intervalo de 1 a 4')

    # 4. Datas, horários e duração
    inicio = data_hora(linha['Start_Time'])
    fim = data_hora(linha['End_Time'])
    duracao = calcular_duracao_minutos(inicio, fim)

    # 5. Cidade e identificação de capital estadual
    cidade = 'Não informado' if ausente(linha['City']) else linha['City']
    eh_capital = (
        estado in CAPITAIS
        and normalizar_cidade(cidade) == normalizar_cidade(CAPITAIS[estado])
    )

    # 6. Montagem explícita do registro transformado
    registro = {
        'ID': identificador,
        'Severity': severidade,
        'severidade_classificacao': CLASSIFICACAO_SEVERIDADE[severidade],
        'is_grave': str(severidade >= 3).lower(),
        'qtd_acidentes': 1,
        'Start_Time': inicio.isoformat(sep=' ') if inicio else '',
        'End_Time': fim.isoformat(sep=' ') if fim else '',
        **extrair_tempo(inicio),
        **extrair_tempo(fim, prefixo='fim_'),
        'duracao_min': duracao,
        'City': cidade,
        'State': estado,
        'is_capital': str(eh_capital).lower(),
        'Visibility(km)': converter_visibilidade_km(linha['Visibility(mi)']),
        'Weather_Condition': clima_pt(linha['Weather_Condition']),
        'Crossing': booleano(linha['Crossing']),
        'Traffic_Signal': booleano(linha['Traffic_Signal']),
    }
    return registro


# ==============================================================================
# 4. AUDITORIA, DEDUPLICAÇÃO E EXECUÇÃO DO PIPELINE ETL
# ==============================================================================

def sha256(caminho):
    """Calcula o hash SHA-256 de um arquivo lendo em blocos de 1 MB."""
    digest = hashlib.sha256()
    with caminho.open('rb') as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b''):
            digest.update(bloco)
    return digest.hexdigest()


def validar_caminhos(entrada, destinos):
    """Garante que a entrada não será sobrescrita e que os arquivos de saída são inéditos."""
    if entrada in destinos or len(set(destinos)) != 3:
        raise ValueError('Entrada e saídas devem ser arquivos diferentes')

    for caminho in destinos:
        if caminho.exists():
            raise FileExistsError('Arquivo já existe: ' + str(caminho))


def validar_cabecalho(nomes_colunas):
    """Verifica se todas as colunas esperadas existem e se não há colunas duplicadas."""
    if not nomes_colunas or not set(ORIGINAIS).issubset(nomes_colunas):
        raise ValueError('Colunas obrigatórias ausentes no cabeçalho')
    if len(nomes_colunas) != len(set(nomes_colunas)):
        raise ValueError('Nomes de colunas duplicados no cabeçalho')


def gerar_assinatura_linha(linha):
    """Gera um hash SHA-256 das colunas originais para detectar duplicatas exatas vs. divergentes."""
    valores_originais = [linha[coluna] for coluna in ORIGINAIS]
    texto_json = json.dumps(valores_originais, ensure_ascii=False)
    return hashlib.sha256(texto_json.encode()).hexdigest()


def executar(entrada, saida):
    """Orquestra a leitura do CSV, transformação, deduplicação via SQLite e geração do relatório."""
    entrada, saida = entrada.resolve(), saida.resolve()
    rejeicoes = saida.with_suffix('.rejeicoes.csv')
    relatorio = saida.with_suffix('.relatorio.json')

    validar_caminhos(entrada, [saida, rejeicoes, relatorio])
    saida.parent.mkdir(parents=True, exist_ok=True)

    hash_antes = sha256(entrada)
    contagem = Counter(lidas=0, vazias=0, aceitas=0, duplicatas_identicas=0, rejeitadas=0)
    motivos = Counter()

    # Usa ExitStack para gerenciar todos os arquivos e o banco SQLite temporário sem aninhamento excessivo.
    # O SQLite em disco guarda os IDs já vistos para não consumir gigabytes de memória RAM.
    with ExitStack() as stack:
        temporario = stack.enter_context(tempfile.TemporaryDirectory(prefix='bdiii_etl_'))
        caminho_sqlite = Path(temporario) / 'ids.sqlite'
        banco = stack.enter_context(closing(sqlite3.connect(str(caminho_sqlite))))
        banco.execute('CREATE TABLE ids (id TEXT PRIMARY KEY, assinatura TEXT NOT NULL)')

        fonte = stack.enter_context(entrada.open(encoding='utf-8-sig', newline=''))
        leitor = csv.DictReader(fonte)
        validar_cabecalho(leitor.fieldnames)

        destino = stack.enter_context(saida.open('x', encoding='utf-8', newline=''))
        rejeitados = stack.enter_context(rejeicoes.open('x', encoding='utf-8', newline=''))

        escritor = csv.DictWriter(destino, fieldnames=COLUNAS, lineterminator='\n')
        erros = csv.writer(rejeitados, lineterminator='\n')
        escritor.writeheader()
        erros.writerow(['linha_csv', 'motivo', 'registro_original_json'])

        for linha in leitor:
            contagem['lidas'] += 1
            try:
                # Linhas com colunas a mais ou a menos geram chave/valor None no DictReader.
                if None in linha or any(v is None for v in linha.values()):
                    raise ValueError('Quantidade de campos diferente do cabeçalho')

                # Ignora linhas completamente em branco.
                if not any(v.strip() for v in linha.values()):
                    contagem['vazias'] += 1
                    continue

                preparado = transformar(linha)
                assinatura = gerar_assinatura_linha(linha)

                # Verifica se o ID já apareceu anteriormente no arquivo.
                anterior = banco.execute(
                    'SELECT assinatura FROM ids WHERE id=?',
                    (preparado['ID'],),
                ).fetchone()

                if anterior:
                    if anterior[0] == assinatura:
                        contagem['duplicatas_identicas'] += 1
                        erros.writerow([
                            leitor.line_num,
                            'Duplicata idêntica removida',
                            json.dumps(linha, ensure_ascii=False),
                        ])
                        continue
                    raise ValueError(
                        'ID duplicado com conteúdo divergente; primeira ocorrência válida mantida para revisão'
                    )

                banco.execute('INSERT INTO ids VALUES (?, ?)', (preparado['ID'], assinatura))
                escritor.writerow(preparado)
                contagem['aceitas'] += 1

            except ValueError as erro:
                contagem['rejeitadas'] += 1
                motivos[str(erro)] += 1
                erros.writerow([leitor.line_num, str(erro), json.dumps(linha, ensure_ascii=False)])

    # Confirma que o arquivo de entrada permaneceu intacto após o processamento.
    hash_depois = sha256(entrada)
    if hash_antes != hash_depois:
        raise RuntimeError('A entrada foi alterada durante a execução')

    assert contagem['lidas'] == sum(
        contagem[c] for c in ('vazias', 'aceitas', 'duplicatas_identicas', 'rejeitadas')
    )

    resumo = {
        'entrada': str(entrada),
        'saida': str(saida),
        'contagens': dict(contagem),
        'motivos_rejeicao': dict(motivos),
        'sha256_original': hash_antes,
        'original_preservado': True,
        'status': 'SEM DADOS VÁLIDOS' if not contagem['aceitas'] else 'CSV GERADO',
        'observacoes': [
            'CSV não armazena tipos: importar datas como TIMESTAMP e booleanos como BOOLEAN.',
            'Valores ausentes opcionais são campos vazios (NULL); City e clima usam Não informado.',
            'is_capital=false para DC; AK e HI são rejeitados por estarem fora do escopo.',
            'IDs divergentes exigem revisão; a primeira ocorrência válida é mantida.',
            'Agrupamento climático remove intensidade e sufixos de vento; snow sempre vira Neve.',
            'Carga de dimensões, geração de chaves e carga da fato no PostgreSQL não executadas.',
        ],
    }

    texto_relatorio = json.dumps(resumo, ensure_ascii=False, indent=2)
    relatorio.write_text(texto_relatorio, encoding='utf-8')
    print(texto_relatorio)


if __name__ == '__main__':
    pasta = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entrada', type=Path, default=pasta / 'US_Accidents_March23.csv')
    parser.add_argument('--saida', type=Path, default=pasta / 'US_Accidents_March23-tratado.csv')
    args = parser.parse_args()
    executar(args.entrada, args.saida)

