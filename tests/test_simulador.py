import csv
from decimal import Decimal
from pathlib import Path

import pytest

from simulador_rtc.classificacao import Classificador, _Regra
from simulador_rtc.cli import main
from simulador_rtc.config import Config
from simulador_rtc.rtc.models import ResultadoItem
from simulador_rtc.simulacao import montar_operacao
from simulador_rtc.sped.parser import EfdIcmsIpiParser

from fake_calculadora import FakeCalculadora

RAIZ = Path(__file__).resolve().parents[1]
EFD = RAIZ / "exemplos" / "efd_icms_ipi_ficticio.txt"
CONFIG = RAIZ / "config" / "simulacao.toml"


# --- parser -----------------------------------------------------------------

def test_parser_le_documentos_itens_e_ncm():
    parser = EfdIcmsIpiParser(EFD)
    docs = list(parser.documentos())

    assert parser.info.empresa.cod_mun == "3505708"
    assert parser.info.empresa.uf == "SP"
    assert [d.numero for d in docs] == ["1001", "2002"]
    assert parser.info.documentos_ignorados == 1  # C100 cancelado (COD_SIT 02)

    cafe = docs[0].itens[1]
    assert cafe.ncm == "09012100"
    assert cafe.vl_desc == Decimal("50.00")
    assert cafe.carga_atual == Decimal("101.50") + Decimal("23.93") + Decimal("110.20")

    refri = docs[1].itens[1]
    assert refri.vl_icms_st == Decimal("64.80")


# --- classificação ----------------------------------------------------------

def test_classificador_prefixo_mais_longo_e_cfop():
    c = Classificador([
        _Regra("", "*", "000", "000001", "padrão"),
        _Regra("1006", "*", "200", "200003", "arroz"),
        _Regra("1006", "5102", "000", "000001", "arroz em 5102"),
    ])
    assert c.classificar("10063021", "1102").cclasstrib == "200003"
    assert c.classificar("10063021", "5102").descricao == "arroz em 5102"
    assert c.classificar("39269090", "1102").descricao == "padrão"


def test_classificador_sem_regra():
    c = Classificador.de_csv(RAIZ / "config" / "classificacao.csv")
    assert c.classificar("39269090", "1102") is None


# --- contrato da API --------------------------------------------------------

def test_operacao_usa_nomes_da_api():
    config = Config.de_toml(CONFIG)
    parser = EfdIcmsIpiParser(EFD)
    doc = next(parser.documentos())
    cls = Classificador.de_csv(config.classificacao_csv)
    op = montar_operacao(doc, parser.info.empresa, config,
                         [(i, cls.classificar(i.ncm, i.cfop)) for i in doc.itens]).para_api()

    assert op["municipio"] == 3505708 and op["uf"] == "SP" and op["tpDoc"] == 55
    assert len(op["id"]) == 32
    item = op["itens"][1]
    assert item["cClassTrib"] == "000001"
    assert item["gComponentesBC"] == {"vProd": 1500.0, "vDesc": 50.0, "vICMS": 101.5,
                                      "vPIS": 23.93, "vCOFINS": 110.2}
    assert item["aliquotasNominais"] == {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05}
    assert '"vPIS": 23.93' in __import__("json").dumps(item)  # sem ruído de ponto flutuante


def test_item_com_imposto_seletivo_envia_grupo_is():
    config = Config.de_toml(CONFIG)
    parser = EfdIcmsIpiParser(EFD)
    doc = list(parser.documentos())[1]
    cls = Classificador.de_csv(config.classificacao_csv)
    op = montar_operacao(doc, parser.info.empresa, config,
                         [(i, cls.classificar(i.ncm, i.cfop)) for i in doc.itens[:2]]).para_api()
    refri = op["itens"][1]
    assert "gComponentesBC" not in refri
    assert refri["impostoSeletivo"] == {"cst": "000", "cClassTrib": "000001", "baseCalculo": 720.0,
                                        "quantidade": 120, "unidade": "UN"}
    assert refri["aliquotasNominais"]["impostoSeletivo"] == {"adValorem": 10.0}
    assert "impostoSeletivo" not in op["itens"][0]["aliquotasNominais"]


def test_resultado_le_resposta_do_exemplo_oficial():
    ok = ResultadoItem.da_api({"nObj": 3, "estadoItem": "CALCULO_SIMULADO", "tribCalc": {"IBSCBS": {
        "gIBSCBS": {"vBC": "1200.00", "vIBS": "0.48", "gCBS": {"vCBS": "42.24"}}}}})
    assert ok.calculado and ok.carga_nova == Decimal("42.72")

    erro = ResultadoItem.da_api({"nObj": 4, "estadoItem": "INCONSISTENCIA_ENTRADA",
                                 "erros": [{"code": "REG-011", "detail": "NCM não vinculada"}]})
    assert not erro.calculado and erro.erros[0].code == "REG-011"


def test_config_exige_aliquotas_a_partir_de_2027(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[calculadora]\nurl="http://x"\n[simulacao]\n'
                   'dh_fato_gerador="2027-01-01T00:00:00-03:00"\nclassificacao_csv="a.csv"\n')
    with pytest.raises(ValueError):
        Config.de_toml(cfg)


# --- ponta a ponta (com o dublê da calculadora) -----------------------------

def test_simulacao_ponta_a_ponta(tmp_path):
    with FakeCalculadora() as fake:
        cfg = tmp_path / "simulacao.toml"
        cfg.write_text(CONFIG.read_text(encoding="utf-8").replace("http://localhost:8080/api", fake.url),
                       encoding="utf-8")
        (tmp_path / "classificacao.csv").write_text(
            (RAIZ / "config" / "classificacao.csv").read_text(encoding="utf-8"), encoding="utf-8")

        saida = tmp_path / "out"
        assert main(["simular", str(EFD), "--config", str(cfg), "--saida", str(saida)]) == 0

    assert len(fake.recebidas) == 2  # um POST por documento
    with (saida / "itens.csv").open(encoding="utf-8") as f:
        itens = list(csv.DictReader(f, delimiter=";"))
    assert len(itens) == 5
    por_cod = {i["cod_item"]: i for i in itens}
    assert por_cod["ARROZ01"]["cbs"] == "0.00"                 # redução de 100% (dublê)
    assert por_cod["ARROZ01"]["carga_nova"] == "42.00"         # só o ICMS remanescente em 2027
    assert por_cod["CAFE01"]["bc_cbs_ibs"] == "1348.50"       # 1500 - 50 - 101,50
    assert por_cod["PECA99"]["estado"] == "SEM_CLASSIFICACAO"
    assert por_cod["REFRI01"]["is"] == "72.00"                # IS 10% (ilustrativo) sobre 720
    assert por_cod["REFRI01"]["bc_cbs_ibs"] == "792.00"       # base derivada: BC(IS) + IS

    with (saida / "pendencias.csv").open(encoding="utf-8") as f:
        pend = list(csv.reader(f, delimiter=";"))
    assert len(pend) == 2 and pend[1][2] == "39269090"
    assert (saida / "resumo_ncm.csv").exists()
