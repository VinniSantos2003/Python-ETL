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
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


# 48 estados contíguos. DC não é capital estadual; AK/HI fora do escopo.
# Referência: https://www.leg.mn.gov/docs/2013/other/130552.pdf
CAPITAIS = dict(item.split('=', 1) for item in (
    'AL=Montgomery|AZ=Phoenix|AR=Little Rock|CA=Sacramento|CO=Denver|'
    'CT=Hartford|DE=Dover|FL=Tallahassee|GA=Atlanta|ID=Boise|IL=Springfield|'
    'IN=Indianapolis|IA=Des Moines|KS=Topeka|KY=Frankfort|LA=Baton Rouge|'
    'ME=Augusta|MD=Annapolis|MA=Boston|MI=Lansing|MN=Saint Paul|MS=Jackson|'
    'MO=Jefferson City|MT=Helena|NE=Lincoln|NV=Carson City|NH=Concord|'
    'NJ=Trenton|NM=Santa Fe|NY=Albany|NC=Raleigh|ND=Bismarck|OH=Columbus|'
    'OK=Oklahoma City|OR=Salem|PA=Harrisburg|RI=Providence|SC=Columbia|'
    'SD=Pierre|TN=Nashville|TX=Austin|UT=Salt Lake City|VT=Montpelier|'
    'VA=Richmond|WA=Olympia|WV=Charleston|WI=Madison|WY=Cheyenne'
).split('|'))
ORIGINAIS = ['ID', 'Severity', 'Start_Time', 'End_Time', 'City', 'State',
             'Visibility(mi)', 'Weather_Condition', 'Crossing', 'Traffic_Signal']
TEMPORAIS = ['ano', 'mes', 'dia', 'dia_semana', 'data', 'hora']
COLUNAS = ['ID', 'Severity', 'severidade_classificacao', 'is_grave',
           'qtd_acidentes', 'Start_Time', 'End_Time'] + TEMPORAIS + [
               'fim_' + c for c in TEMPORAIS] + [
                   'duracao_min', 'City', 'State', 'is_capital',
                   'Visibility(km)', 'Weather_Condition', 'Crossing', 'Traffic_Signal']
DIAS = ['Segunda-feira', 'Terça-feira', 'Quarta-feira', 'Quinta-feira',
        'Sexta-feira', 'Sábado', 'Domingo']
AUSENTES = {'', 'nan', 'null', 'none', 'n/a', 'na'}


def ausente(valor):
    return valor.strip().casefold() in AUSENTES


def normalizar_cidade(valor):
    # Apenas para o cruzamento; City é preservada na saída.
    return ' '.join(valor.casefold().replace('.', '').split()).replace('st ', 'saint ')


def clima_pt(valor):
    """Agrupamentos explícitos; categorias desconhecidas vão para rejeições."""
    if ausente(valor):
        return 'Não informado'
    texto = valor.strip().casefold()
    # A regra de neve tem precedência, inclusive em chuva misturada com neve.
    if 'snow' in texto:
        return 'Neve'
    traducoes = {
        'fair': 'Tempo bom', 'clear': 'Céu limpo', 'partly cloudy': 'Parcialmente nublado',
        'mostly cloudy': 'Predominantemente nublado', 'cloudy': 'Nublado',
        'overcast': 'Encoberto', 'thunder': 'Trovões', 't-storm': 'Tempestade',
        't-storms': 'Tempestade', 'thunderstorm': 'Tempestade',
        'thunderstorms': 'Tempestade', 'thunderstorms and rain': 'Tempestade com chuva',
        'rain': 'Chuva', 'drizzle': 'Garoa', 'light freezing rain': 'Chuva congelante leve',
        'freezing rain': 'Chuva congelante', 'freezing drizzle': 'Garoa congelante',
        'fog': 'Nevoeiro', 'shallow fog': 'Nevoeiro raso', 'patches of fog': 'Bancos de nevoeiro',
        'haze': 'Névoa seca', 'mist': 'Névoa úmida', 'smoke': 'Fumaça',
        'sleet': 'Chuva de gelo', 'ice pellets': 'Grãos de gelo',
        'hail': 'Granizo', 'small hail': 'Granizo pequeno', 'wintry mix': 'Precipitação invernal mista',
        'blowing dust': 'Poeira suspensa pelo vento', 'widespread dust': 'Poeira generalizada',
        'sand': 'Areia', 'blowing sand': 'Areia suspensa pelo vento',
        'dust whirls': 'Redemoinhos de poeira', 'sand / dust whirls nearby': 'Redemoinhos de areia ou poeira próximos',
        'squalls': 'Rajadas de vento', 'tornado': 'Tornado', 'volcanic ash': 'Cinzas vulcânicas',
        'funnel cloud': 'Nuvem funil', 'blowing spray': 'Borrifos de água pelo vento',
    }
    # Intensidade e vento são agrupados; o fenômeno principal é mantido.
    texto = re.sub(r'\s*/\s*(windy|wind)\s*$', '', texto)
    texto = re.sub(r'^(light|heavy)\s+', '', texto)
    proximidade = ''
    for prefixo in ('conditions of ',):
        texto = texto.removeprefix(prefixo)
    if texto.endswith(' in the vicinity'):
        texto = texto.removesuffix(' in the vicinity')
        proximidade = ' nas proximidades'
    if texto.endswith(' nearby') and texto not in traducoes:
        texto = texto.removesuffix(' nearby')
        proximidade = ' nas proximidades'
    if texto not in traducoes:
        raise ValueError('Condição climática sem tradução definida: ' + valor)
    return traducoes[texto] + proximidade


def booleano(valor):
    if ausente(valor):
        return ''
    tabela = {'true': 'true', 'false': 'false', '1': 'true', '0': 'false'}
    try:
        return tabela[valor.strip().casefold()]
    except KeyError:
        raise ValueError('Booleano inválido: ' + valor) from None


def data_hora(valor):
    if ausente(valor):
        return None
    # PostgreSQL TIMESTAMP tem precisão de microssegundos. Trunca frações extras.
    texto = re.sub(r'(\.\d{6})\d+', r'\1', valor.strip())
    try:
        resultado = datetime.fromisoformat(texto)
    except ValueError:
        raise ValueError('Data/hora inválida: ' + valor) from None
    if resultado.tzinfo is not None:
        raise ValueError('Timestamp com fuso: requer decisão explícita de conversão')
    return resultado


def extrair_tempo(instante):
    if instante is None:
        return ['', '', '', '', '', '']
    return [instante.year, instante.month, instante.day, DIAS[instante.weekday()],
            instante.date().isoformat(), instante.hour]


def transformar(linha):
    identificador = linha['ID'].strip()
    if ausente(identificador):
        raise ValueError('ID ausente')
    estado = linha['State'].strip().upper()
    if estado not in CAPITAIS and estado != 'DC':
        raise ValueError('Estado ausente, inválido ou fora do escopo: ' + estado)
    try:
        severidade = int(linha['Severity'])
    except ValueError:
        raise ValueError('Severity ausente ou inválida') from None
    if severidade not in (1, 2, 3, 4):
        raise ValueError('Severity fora do intervalo de 1 a 4')
    inicio, fim = (data_hora(linha[c]) for c in ('Start_Time', 'End_Time'))
    duracao = ''
    if inicio is not None and fim is not None:
        delta = fim - inicio
        if delta.total_seconds() < 0:
            raise ValueError('End_Time anterior a Start_Time')
        micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        duracao = format(Decimal(micros) / Decimal(60000000), 'f')
    visibilidade = ''
    if not ausente(linha['Visibility(mi)']):
        try:
            milhas = Decimal(linha['Visibility(mi)'].strip())
        except InvalidOperation:
            raise ValueError('Visibilidade inválida') from None
        if not milhas.is_finite() or milhas < 0:
            raise ValueError('Visibilidade deve ser finita e não negativa')
        visibilidade = format(milhas * Decimal('1.609344'), 'f')
    cidade = 'Não informado' if ausente(linha['City']) else linha['City']
    capital = estado in CAPITAIS and normalizar_cidade(cidade) == normalizar_cidade(CAPITAIS[estado])
    valores = [identificador, severidade, {1: 'Leve', 2: 'Moderada', 3: 'Grave', 4: 'Grave'}[severidade],
               str(severidade >= 3).lower(), 1,
               inicio.isoformat(sep=' ') if inicio else '', fim.isoformat(sep=' ') if fim else '']
    valores += extrair_tempo(inicio) + extrair_tempo(fim)
    valores += [duracao, cidade, estado, str(capital).lower(), visibilidade,
                clima_pt(linha['Weather_Condition']), booleano(linha['Crossing']),
                booleano(linha['Traffic_Signal'])]
    return dict(zip(COLUNAS, valores))


def sha256(caminho):
    digest = hashlib.sha256()
    with caminho.open('rb') as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b''):
            digest.update(bloco)
    return digest.hexdigest()


def executar(entrada, saida):
    entrada, saida = entrada.resolve(), saida.resolve()
    rejeicoes = saida.with_suffix('.rejeicoes.csv')
    relatorio = saida.with_suffix('.relatorio.json')
    destinos = [saida, rejeicoes, relatorio]
    if entrada in destinos or len(set(destinos)) != 3:
        raise ValueError('Entrada e saídas devem ser arquivos diferentes')
    # Não sobrescreve nenhum resultado anterior por acidente.
    for caminho in destinos:
        if caminho.exists():
            raise FileExistsError('Arquivo já existe: ' + str(caminho))
    hash_antes = sha256(entrada)
    contagem = Counter(lidas=0, vazias=0, aceitas=0, duplicatas_identicas=0, rejeitadas=0)
    motivos = Counter()
    saida.parent.mkdir(parents=True, exist_ok=True)
    # Índice de IDs em disco, para não manter milhões de registros na memória.
    with tempfile.TemporaryDirectory(prefix='bdiii_etl_') as temporario:
        with closing(sqlite3.connect(str(Path(temporario) / 'ids.sqlite'))) as banco:
            banco.execute('CREATE TABLE ids (id TEXT PRIMARY KEY, assinatura TEXT NOT NULL)')
            with entrada.open(encoding='utf-8-sig', newline='') as fonte:
                leitor = csv.DictReader(fonte)
                if not leitor.fieldnames or not set(ORIGINAIS).issubset(leitor.fieldnames):
                    raise ValueError('Colunas obrigatórias ausentes no cabeçalho')
                if len(leitor.fieldnames) != len(set(leitor.fieldnames)):
                    raise ValueError('Nomes de colunas duplicados no cabeçalho')
                with saida.open('x', encoding='utf-8', newline='') as destino, rejeicoes.open(
                        'x', encoding='utf-8', newline='') as rejeitados:
                    escritor = csv.DictWriter(destino, fieldnames=COLUNAS, lineterminator='\n')
                    erros = csv.writer(rejeitados, lineterminator='\n')
                    escritor.writeheader()
                    erros.writerow(['linha_csv', 'motivo', 'registro_original_json'])
                    for linha in leitor:
                        contagem['lidas'] += 1
                        try:
                            if None in linha or any(v is None for v in linha.values()):
                                raise ValueError('Quantidade de campos diferente do cabeçalho')
                            if not any(v.strip() for v in linha.values()):
                                contagem['vazias'] += 1
                                continue
                            preparado = transformar(linha)
                            # Compara as variáveis selecionadas, antes de qualquer agrupamento.
                            assinatura = hashlib.sha256(json.dumps(
                                [linha[c] for c in ORIGINAIS], ensure_ascii=False).encode()).hexdigest()
                            anterior = banco.execute('SELECT assinatura FROM ids WHERE id=?',
                                                     (preparado['ID'],)).fetchone()
                            if anterior:
                                if anterior[0] == assinatura:
                                    contagem['duplicatas_identicas'] += 1
                                    erros.writerow([leitor.line_num, 'Duplicata idêntica removida',
                                                    json.dumps(linha, ensure_ascii=False)])
                                    continue
                                raise ValueError('ID duplicado com conteúdo divergente; primeira ocorrência válida mantida para revisão')
                            banco.execute('INSERT INTO ids VALUES (?, ?)', (preparado['ID'], assinatura))
                            escritor.writerow(preparado)
                            contagem['aceitas'] += 1
                        except ValueError as erro:
                            contagem['rejeitadas'] += 1
                            motivos[str(erro)] += 1
                            erros.writerow([leitor.line_num, str(erro), json.dumps(linha, ensure_ascii=False)])
    hash_depois = sha256(entrada)
    if hash_antes != hash_depois:
        raise RuntimeError('A entrada foi alterada durante a execução')
    assert contagem['lidas'] == sum(contagem[c] for c in (
        'vazias', 'aceitas', 'duplicatas_identicas', 'rejeitadas'))
    resumo = {'entrada': str(entrada), 'saida': str(saida), 'contagens': dict(contagem),
              'motivos_rejeicao': dict(motivos), 'sha256_original': hash_antes,
              'original_preservado': True,
              'status': 'SEM DADOS VÁLIDOS' if not contagem['aceitas'] else 'CSV GERADO',
              'observacoes': [
                  'CSV não armazena tipos: importar datas como TIMESTAMP e booleanos como BOOLEAN.',
                  'Valores ausentes opcionais são campos vazios (NULL); City e clima usam Não informado.',
                  'is_capital=false para DC; AK e HI são rejeitados por estarem fora do escopo.',
                  'IDs divergentes exigem revisão; a primeira ocorrência válida é mantida.',
                  'Agrupamento climático remove intensidade e sufixos de vento; snow sempre vira Neve.',
                  'Carga de dimensões, geração de chaves e carga da fato no PostgreSQL não executadas.'
              ]}
    relatorio.write_text(json.dumps(resumo, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(resumo, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    pasta = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entrada', type=Path, default=pasta / 'US_Accidents_March23.csv')
    parser.add_argument('--saida', type=Path, default=pasta / 'US_Accidents_March23-tratado.csv')
    args = parser.parse_args()
    executar(args.entrada, args.saida)
