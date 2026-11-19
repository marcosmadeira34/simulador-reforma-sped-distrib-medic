# Simulador de impacto da Reforma Tributária a partir do SPED

Lê uma **EFD ICMS/IPI** ou **XMLs de NF-e**, classifica cada item (CST e cClassTrib da CBS/IBS), calcula **CBS, IBS e Imposto Seletivo** na [Calculadora RTC oficial da Receita Federal](https://github.com/ReceitaFederal/calculadora-rtc-regime-regular) e compara com a carga atual (ICMS, ICMS-ST, IPI, PIS e COFINS), item a item e por NCM.

> Projeto de estudo e portfólio. Os resultados são simulações: as regras de classificação de exemplo são ilustrativas e precisam de revisão da área tributária.

## Arquitetura

```mermaid
flowchart LR
    A[EFD ICMS/IPI<br/>.txt] --> B[Parser em streaming<br/>0000 · 0200 · C100 · C170]
    B --> C[Classificador<br/>de-para NCM/CFOP → CST/cClassTrib]
    C -->|sem regra| P[pendencias.csv]
    C --> D[Montador da operação<br/>gComponentesBC + alíquotas nominais]
    D -->|1 POST por documento<br/>em paralelo| E[(Calculadora RTC<br/>Java 21 · local)]
    E -->|resultado por item<br/>observabilidade| F[Comparador<br/>carga atual × carga simulada]
    F --> G[itens.csv]
    F --> H[resumo_ncm.csv]
    F --> P
```

Decisões principais:

- **Streaming:** o arquivo é lido linha a linha; um documento (C100 + seus C170) é enviado por vez, com uma janela limitada de envios paralelos. Funciona com arquivos de vários GB sem estourar memória.
- **Endpoint de observabilidade** (`POST /observabilidade/regime-geral`): um item com erro não derruba o documento inteiro; cada erro vem com código (ex.: `REG-011`) e vai para `pendencias.csv`.
- **Base de cálculo pela própria calculadora:** o simulador envia os componentes (`gComponentesBC`: valor do item, desconto, ICMS, PIS, COFINS) e a calculadora aplica a vigência de cada um à data simulada. Assim a regra de base não é reimplementada no Python.
- **De-para em CSV:** a classificação tributária fica em `config/classificacao.csv`, editável pela área fiscal sem mexer em código, e cada linha do relatório diz qual regra foi usada.
- **Tributos remanescentes configuráveis:** em 2027, por exemplo, o ICMS continua integral. A "carga simulada" soma CBS + IBS + IS + os tributos atuais que ainda existem no cenário.

## Setor de saúde: distribuidora de medicamentos e produtos hospitalares (v0.2)

O motor `setores/saude.py` aplica duas lógicas, conforme o que a calculadora consegue validar:

| Tipo de item | O que decide o tratamento | Fonte |
|---|---|---|
| Medicamentos, soros e vacinas | registro na Anvisa, lista de alíquota zero, tipo de comprador | cadastro de produtos, grupo `med/cProdANVISA` da NF-e e cadastro de participantes |
| Dispositivos médicos, acessibilidade, nutrição | NCM nos Anexos IV, V, VI, XII e XIII | endpoint `ncm-aplicavel` da calculadora (com cache) |

Achado que motivou essa divisão: na calculadora 1.6.1, os cClassTrib de medicamentos aceitam **qualquer NCM** (inclusive seringa e cadeira de rodas), enquanto os de dispositivos e acessibilidade aceitam só os NCMs dos anexos. Exemplos confirmados na calculadora: seringa `90183119` → 200030 (Anexo IV, 60%); cadeira de rodas `87139000` → 200007 (Anexo XIII, alíquota zero).

O `ncm-aplicavel` confere só a lista do anexo e responde `valido=true` até para NCM extinto (ex.: `40151100`). Por isso o simulador consulta `GET /dados-abertos/ncm` antes de classificar (com cache por NCM) e manda NCMs inexistentes para `pendencias.csv` com estado `NCM_INEXISTENTE`. Para desligar, use `validar_ncm = false` em `[simulacao]`.

Regras, em ordem de prioridade:

1. **Medicamento ou soro/vacina sem registro na Anvisa** → tributação integral e alerta `MEDICAMENTO_SEM_REGISTRO_ANVISA`.
2. **Comprador órgão público** → 200010. **Entidade de saúde imune (CEBAS)** → 200010 com alerta `CONFIRMAR_CODIGO_ENTIDADE_IMUNE` (a LC 214 prevê alíquota zero, mas a descrição do código cita só órgãos públicos).
3. **Soro ou vacina** → 200053. **Medicamento na lista de alíquota zero** → 200009. **Demais medicamentos** → 200032 (60%).
4. **Dispositivo, acessibilidade ou nutrição** → primeira classificação aceita pelo `ncm-aplicavel`, alíquota zero antes de 60%: 200004 → 200005 → 200030; 200007 → 200008 → 200031; 200011 → 200033. Classificações exclusivas de órgão público ou entidade imune só são testadas para esses compradores.
5. **Produto sem cadastro** → tenta todos os anexos pelo NCM, com alertas `PRODUTO_SEM_CADASTRO` e `CLASSIFICADO_SO_PELO_NCM`.

Rodando o exemplo (3 NF-e fictícias: hospital privado, prefeitura e Santa Casa):

```bash
simulador-rtc simular exemplos/saude/nfe --config config/saude.toml --saida resultados_saude
```

Arquivos de apoio: `config/saude.toml`, `exemplos/saude/cadastro_produtos.csv` e `exemplos/saude/participantes.csv`.

> **Atenção ao PIS/COFINS monofásico:** na distribuidora, medicamentos saem com PIS/COFINS zerado (CST 04), porque o tributo foi pago pela indústria e está embutido no preço. Por isso a carga simulada de um medicamento com redução de 60% pode ficar **maior** que a atual nessa etapa da cadeia, sem que a carga da cadeia toda aumente. Para uma comparação justa, analise a cadeia (indústria + distribuidora) ou o custo efetivo.

## Como rodar

### 1. Suba a calculadora oficial

Com Docker (constrói a partir do repositório da Receita):

```bash
docker compose up -d --build
```

Ou manualmente, seguindo o README da calculadora (Java 21):

```bash
git clone https://github.com/ReceitaFederal/calculadora-rtc-regime-regular.git
cd calculadora-rtc-regime-regular
mkdir -p ./calculadora/db
./mvnw flyway:migrate -Dflyway.configFiles=./flyway/flyway-pro.conf --batch-mode --errors
./mvnw spring-boot:run -Dspring-boot.run.profiles=offline
```

A API fica em `http://localhost:8080/api` (Swagger em `/api/swagger-ui.html`).

### 2. Instale e rode o simulador

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

simulador-rtc ping
simulador-rtc simular exemplos/efd_icms_ipi_ficticio.txt --saida resultados
```

Saída no terminal (exemplo com o arquivo fictício):

```
Itens: 5 | calculados: 4 | pendentes: 1 | documentos ignorados (cancelados etc.): 1
Carga atual (ICMS+ST+IPI+PIS+COFINS): R$ 3,844.53
Carga simulada (CBS+IBS+IS+ICMS+ICMS_ST): R$ ...
```

### 3. Testes

```bash
pytest
```

Os testes usam um **dublê** da calculadora (`tests/fake_calculadora.py`) que aplica uma regra simplificada só para validar o fluxo. Os valores reais vêm da calculadora oficial.

## Configuração (`config/simulacao.toml`)

| Chave | Para quê |
|---|---|
| `calculadora.url` | Endereço da calculadora local |
| `calculadora.workers` | Documentos enviados em paralelo |
| `simulacao.dh_fato_gerador` | Data simulada; a calculadora aplica as regras vigentes nessa data |
| `simulacao.aliquotas_nominais` | Obrigatórias a partir de 2027 (proibidas antes). Os valores de exemplo vêm da documentação oficial da calculadora |
| `simulacao.tributos_remanescentes` | Tributos atuais que continuam no cenário (ex.: `["ICMS", "ICMS_ST"]` em 2027) |
| `simulacao.classificacao_csv` | Arquivo de de-para NCM/CFOP → CST/cClassTrib |

## Saídas

| Arquivo | Conteúdo |
|---|---|
| `itens.csv` | Uma linha por item: tributos atuais, base e valores de CBS/IBS/IS, regra de classificação usada, estado do cálculo |
| `resumo_ncm.csv` | Carga atual × simulada por NCM, em R$ e em % do valor dos itens |
| `pendencias.csv` | Itens sem classificação ou com erro da calculadora, com o código e a descrição do erro |
| `revisao.csv` | Itens calculados, mas com alerta para revisão humana (ex.: medicamento sem registro na Anvisa) |
| `resumo_tratamento.csv` | Carga atual × simulada por cClassTrib (tratamento tributário) |

## Limitações conhecidas (v0.1)

- Na EFD ICMS/IPI, lê só o C170, que normalmente existe nas **entradas** e nas saídas que não são NF-e própria. Para as saídas próprias, use os XMLs de NF-e.
- Na EFD, frete, seguro e outras despesas do C100 ainda não são rateados aos itens (na NF-e eles já vêm por item).
- A lista de medicamentos com alíquota zero vem do cadastro (`aliquota_zero = S`); o simulador não confere a substância contra a LC 214.
- Imposto Seletivo: ligado pelas colunas `is_*` do de-para, com alíquota ad valorem simulada. Alíquota ad rem e unidade tributável específica ainda não são tratadas.
- A calculadora evolui a cada release e as tabelas têm vigência: registre a versão usada (`RTC_REF` no `docker-compose.yml`).

## Roadmap

- [x] Leitor de XML de NF-e para as saídas próprias
- [x] Motor de regras do setor de saúde (perfil distribuidora)
- [ ] Perfis hospital e indústria no setor de saúde
- [ ] EFD-Contribuições (créditos de PIS/COFINS → CBS)
- [ ] Indicadores de crédito da calculadora (apropriação, crédito presumido) para estimar créditos nas compras
- [ ] Rateio de frete, seguro e outras despesas por item
- [ ] Dashboard (Streamlit) com o resumo por NCM e CFOP
- [ ] Sugestão de cClassTrib a partir das tabelas de dados abertos

## Dados

Use apenas arquivos **fictícios ou anonimizados**. O `.gitignore` já bloqueia a pasta `dados/` e arquivos `.sped`. O arquivo em `exemplos/` é inteiramente inventado.

## Licença

MIT. A Calculadora RTC é da Receita Federal do Brasil, também sob licença MIT.
