"""Testes de contrato: o que o simulador espera da Calculadora RTC.

Cada teste roda contra o dublê (sempre) e contra a calculadora real quando a
variável RTC_URL estiver definida:

    RTC_URL=http://localhost:8080/api pytest -m contrato

Use-os antes de subir a versão da calculadora: se algum falhar na real, o
comportamento mudou e o simulador (ou o dublê) precisa ser revisto antes de
atualizar `versao_esperada`. O dublê roda os mesmos testes para não divergir
da real sem ninguém perceber.
"""

import csv
import os
from decimal import Decimal
from pathlib import Path

import pytest

from simulador_rtc.cli import main
from simulador_rtc.rtc.client import CalculadoraClient
from simulador_rtc.rtc.models import AliquotasNominais, ComponentesBC, ItemOperacao, Operacao

from fake_calculadora import FakeCalculadora

pytestmark = pytest.mark.contrato

RAIZ = Path(__file__).resolve().parents[1]
REFERENCIA = Path(__file__).parent / "referencia" / "saude_2027.csv"
RTC_URL = os.environ.get("RTC_URL", "")
VERSAO = "1.6.1"


@pytest.fixture(scope="module", params=["dublê", "real"])
def url(request):
    if request.param == "real":
        if not RTC_URL:
            pytest.skip("defina RTC_URL para rodar contra a calculadora real")
        yield RTC_URL
    else:
        with FakeCalculadora() as fake:
            yield fake.url


@pytest.fixture
def cliente(url):
    return CalculadoraClient(url, tentativas=1)


def _real():
    if not RTC_URL:
        pytest.skip("defina RTC_URL para rodar contra a calculadora real")
    return CalculadoraClient(RTC_URL, tentativas=1)


def _calcular(cliente, data, cclass="000001", cst="000", aliquotas=True, v_icms="180"):
    aliq = AliquotasNominais(cbs=Decimal("8.8"), ibsEstadual=Decimal("0.05"), ibsMunicipal=Decimal("0.05")) \
        if aliquotas else None
    op = Operacao(id="contrato", dhFatoGerador=f"{data}T10:00:00-03:00", municipio=3550308, uf="SP", tpDoc=55,
                  itens=[ItemOperacao(numero=1, ncm="90183119", cst=cst, cClassTrib=cclass,
                                      quantidade=Decimal("1"), unidade="UN", aliquotasNominais=aliq,
                                      gComponentesBC=ComponentesBC(vProd=Decimal("1000"), vICMS=Decimal(v_icms)))])
    return cliente.calcular(op)[0]


# --- versão e tabelas ---------------------------------------------------------

def test_versao_fixada(cliente):
    assert cliente.versao()["versaoApp"] == VERSAO


def test_ncm_existe_confere_tabela_vigente(cliente):
    assert cliente.ncm_existe("30024121", "2027-01-15")       # vacina contra a gripe
    assert not cliente.ncm_existe("40151100", "2027-01-15")   # luva: NCM extinto
    assert not cliente.ncm_existe("30022029", "2027-01-15")


def test_ncm_aplicavel_discrimina_listas_de_ncm(cliente):
    d = "2027-01-15"
    assert cliente.ncm_aplicavel("200032", "90183119", d)       # medicamento aceita qualquer NCM
    assert cliente.ncm_aplicavel("200030", "90183119", d)       # seringa: Anexo IV
    assert cliente.ncm_aplicavel("200007", "87139000", d)       # cadeira de rodas: Anexo XIII
    assert not cliente.ncm_aplicavel("200030", "39269090", d)   # plástico fora do anexo


def test_indicadores_de_credito_do_adquirente(cliente):
    d = "2027-01-15"
    assert cliente.credito_adquirente("000001", d) == (True, True)
    assert cliente.credito_adquirente("200030", d) == (True, True)
    assert cliente.credito_adquirente("200053", d) == (False, False)  # soros e vacinas


def test_fundamentacao_legal(cliente):
    d = "2027-01-15"
    assert cliente.fundamentacao("200032", d) == "LC 214/2025, Art. 133"
    assert cliente.fundamentacao("200053", d) == "LC 214/2025, Art. 146, § 1º, III"
    assert cliente.fundamentacao("000001", d) == "LC 214/2025"


# --- cálculo por ano ----------------------------------------------------------

def test_2026_usa_aliquotas_de_teste(cliente):
    r = _calcular(cliente, "2026-06-15", aliquotas=False)
    assert r.estado == "CALCULADO"
    assert (r.v_bc, r.v_cbs, r.v_ibs) == (Decimal("820.00"), Decimal("7.38"), Decimal("0.82"))


def test_2026_recusa_aliquotas_nominais(cliente):
    assert _calcular(cliente, "2026-06-15").erros[0].code == "REG-036"


def test_2027_exige_aliquotas_nominais(cliente):
    assert _calcular(cliente, "2027-06-15", aliquotas=False).erros[0].code == "REG-037"


def test_2027_integral_abate_icms_da_base(cliente):
    r = _calcular(cliente, "2027-06-15")
    assert r.estado == "CALCULO_SIMULADO"
    assert (r.v_bc, r.v_cbs, r.v_ibs) == (Decimal("820.00"), Decimal("72.16"), Decimal("0.82"))


def test_reducao_de_60(cliente):
    r = _calcular(cliente, "2027-06-15", cclass="200030", cst="200")
    assert r.v_cbs == Decimal("28.86")


def test_2033_icms_sai_da_base(cliente):
    assert _calcular(cliente, "2033-06-15").v_bc == Decimal("1000.00")


# --- só na calculadora real ---------------------------------------------------

def test_real_ncm_aplicavel_aceita_ncm_extinto():
    """Motivo do `validar_ncm`: se um dia isto falhar, a calculadora passou a conferir sozinha."""
    assert _real().ncm_aplicavel("200030", "40151100", "2027-01-15")


CAMPOS_REFERENCIA = ["documento", "num_item", "cod_item", "natureza", "estado", "cclasstrib",
                     "bc_cbs_ibs", "cbs", "ibs", "credito_novo"]


def _referencia(itens_csv: Path) -> list[dict[str, str]]:
    with itens_csv.open(encoding="utf-8") as f:
        return [{k: l[k] for k in CAMPOS_REFERENCIA} for l in csv.DictReader(f, delimiter=";")]


def test_real_exemplo_saude_bate_com_a_referencia(tmp_path):
    """Resultado de referência da distribuidora (EFD de entradas + NF-e de saída, 2027).

    Gerado na calculadora 1.6.1. Quando a área tributária aprovar os valores, qualquer
    diferença numa versão nova da calculadora precisa ser explicada antes de adotá-la.
    """
    _real()
    saida = tmp_path / "out"
    cfg = tmp_path / "saude.toml"
    texto = (RAIZ / "config" / "saude.toml").read_text(encoding="utf-8")
    texto = texto.replace("http://localhost:8080/api", RTC_URL)
    texto = texto.replace("../exemplos/saude/", str(RAIZ / "exemplos" / "saude") + "/")
    cfg.write_text(texto, encoding="utf-8")
    assert main(["simular", str(RAIZ / "exemplos" / "saude" / "efd_icms_ipi_outubro.txt"),
                 str(RAIZ / "exemplos" / "saude" / "nfe"), "--config", str(cfg), "--saida", str(saida)]) == 0
    with REFERENCIA.open(encoding="utf-8") as f:
        esperado = list(csv.DictReader((l for l in f if not l.startswith("#")), delimiter=";"))
    assert _referencia(saida / "itens.csv") == esperado
