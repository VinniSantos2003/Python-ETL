"""Bulk-load treated CSV into the dedicated Compose PostgreSQL database."""
import argparse, shutil, subprocess
from pathlib import Path
CONTAINER="etl-marco2-postgres"; USER="etl"; DB="acidentes"
COLS="""id severity severidade_classificacao is_grave qtd_acidentes start_time end_time ano mes dia dia_semana data hora fim_ano fim_mes fim_dia fim_dia_semana fim_data fim_hora duracao_min city state is_capital visibility_km weather_condition crossing traffic_signal""".split()
DDL="""
CREATE TABLE IF NOT EXISTS dim_tempo(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,start_time DATE NOT NULL,end_time DATE NOT NULL,UNIQUE(start_time,end_time));
CREATE TABLE IF NOT EXISTS dim_localizacao(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,city VARCHAR(200) NOT NULL,state VARCHAR(10) NOT NULL,is_capital BOOLEAN NOT NULL,CONSTRAINT uq_dim_localizacao UNIQUE NULLS NOT DISTINCT(city,state,is_capital));
CREATE TABLE IF NOT EXISTS dim_clima(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,weather_condition VARCHAR(200) NOT NULL,visibility VARCHAR(40),CONSTRAINT uq_dim_clima UNIQUE NULLS NOT DISTINCT(weather_condition,visibility));
CREATE TABLE IF NOT EXISTS dim_infraestrutura(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,traffic_signal BOOLEAN,crossing BOOLEAN,CONSTRAINT uq_dim_infraestrutura UNIQUE NULLS NOT DISTINCT(traffic_signal,crossing));
CREATE TABLE IF NOT EXISTS fato_acidente(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,tempo_id BIGINT NOT NULL REFERENCES dim_tempo(id),localizacao_id BIGINT NOT NULL REFERENCES dim_localizacao(id),clima_id BIGINT NOT NULL REFERENCES dim_clima(id),infraestrutura_id BIGINT NOT NULL REFERENCES dim_infraestrutura(id),severity INTEGER NOT NULL);
DO $$ BEGIN IF EXISTS(SELECT 1 FROM fato_acidente) THEN RAISE EXCEPTION 'fato_acidente already has rows; refusing duplicate load'; END IF; END $$;
CREATE TEMP TABLE etl_stage(id TEXT,severity TEXT,severidade_classificacao TEXT,is_grave TEXT,qtd_acidentes TEXT,start_time TEXT,end_time TEXT,ano TEXT,mes TEXT,dia TEXT,dia_semana TEXT,data TEXT,hora TEXT,fim_ano TEXT,fim_mes TEXT,fim_dia TEXT,fim_dia_semana TEXT,fim_data TEXT,fim_hora TEXT,duracao_min TEXT,city TEXT,state TEXT,is_capital TEXT,visibility_km TEXT,weather_condition TEXT,crossing TEXT,traffic_signal TEXT);
"""
SQL="""
INSERT INTO dim_tempo(start_time,end_time) SELECT DISTINCT left(start_time,10)::date,left(end_time,10)::date FROM etl_stage ON CONFLICT(start_time,end_time) DO NOTHING;
INSERT INTO dim_localizacao(city,state,is_capital) SELECT DISTINCT COALESCE(NULLIF(city,''),'Nao informado'),COALESCE(NULLIF(state,''),'Nao informado'),COALESCE(NULLIF(is_capital,'')::boolean,false) FROM etl_stage ON CONFLICT(city,state,is_capital) DO NOTHING;
INSERT INTO dim_clima(weather_condition,visibility) SELECT DISTINCT COALESCE(NULLIF(weather_condition,''),'Nao informado'),NULLIF(visibility_km,'') FROM etl_stage ON CONFLICT(weather_condition,visibility) DO NOTHING;
INSERT INTO dim_infraestrutura(traffic_signal,crossing) SELECT DISTINCT NULLIF(traffic_signal,'')::boolean,NULLIF(crossing,'')::boolean FROM etl_stage ON CONFLICT(traffic_signal,crossing) DO NOTHING;
INSERT INTO fato_acidente(tempo_id,localizacao_id,clima_id,infraestrutura_id,severity)
SELECT t.id,l.id,c.id,i.id,s.severity::integer FROM etl_stage s
JOIN dim_tempo t ON t.start_time=left(s.start_time,10)::date AND t.end_time=left(s.end_time,10)::date
JOIN dim_localizacao l ON l.city=COALESCE(NULLIF(s.city,''),'Nao informado') AND l.state=COALESCE(NULLIF(s.state,''),'Nao informado') AND l.is_capital=COALESCE(NULLIF(s.is_capital,'')::boolean,false)
JOIN dim_clima c ON c.weather_condition=COALESCE(NULLIF(s.weather_condition,''),'Nao informado') AND c.visibility IS NOT DISTINCT FROM NULLIF(s.visibility_km,'')
JOIN dim_infraestrutura i ON i.traffic_signal IS NOT DISTINCT FROM NULLIF(s.traffic_signal,'')::boolean AND i.crossing IS NOT DISTINCT FROM NULLIF(s.crossing,'')::boolean;
SELECT 'dim_tempo' tabela,count(*) FROM dim_tempo UNION ALL SELECT 'dim_localizacao',count(*) FROM dim_localizacao UNION ALL SELECT 'dim_clima',count(*) FROM dim_clima UNION ALL SELECT 'dim_infraestrutura',count(*) FROM dim_infraestrutura UNION ALL SELECT 'fato_acidente',count(*) FROM fato_acidente ORDER BY 1;
"""
def main():
 base=Path(__file__).resolve().parent; ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--csv",type=Path,default=base/"US_Accidents_March23-tratado.csv"); ap.add_argument("--container",default=CONTAINER); a=ap.parse_args()
 if not a.csv.is_file(): ap.error(f"CSV not found: {a.csv}")
 p=subprocess.Popen(["docker","exec","-i",a.container,"psql","-X","-v","ON_ERROR_STOP=1","-U",USER,"-d",DB],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 try:
  p.stdin.write((DDL+"\\copy etl_stage ("+",".join(COLS)+") FROM STDIN WITH (FORMAT csv, HEADER true)\n").encode())
  with a.csv.open("rb") as f: shutil.copyfileobj(f,p.stdin,length=8*1024*1024)
  p.stdin.write(b"\\.\nANALYZE etl_stage;\n"+SQL.encode()); p.stdin.close()
 except Exception: p.kill(); raise
 out=p.stdout.read().decode(errors="replace"); err=p.stderr.read().decode(errors="replace"); rc=p.wait()
 if rc: raise SystemExit(f"psql failed ({rc}):\n{err}\n{out}")
 print(out); print(f"Load completed from {a.csv}")
if __name__=="__main__": main()
