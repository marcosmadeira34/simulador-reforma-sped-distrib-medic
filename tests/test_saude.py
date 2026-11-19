import csv
from decimal import Decimal
from pathlib import Path

from simulador_rtc.cli import main
from simulador_rtc.modelos import Documento, Item, Participante
from simulador_rtc.nfe.parser import NfeParser
from simulador_rtc.rtc.client import CalculadoraClient
from simulador_rtc.setores.saude import ClassificadorSaude, Produto, TipoComprador, TipoProduto

from fake_calculadora import FakeCalculadora

RAIZ = Path(__file__).resolve().parents[1]
NFE = RAIZ / "exemplos" / "saude" / "nfe"
CONFIG = RAIZ / "config" / "saude.toml"
Z = Decimal("0")


def _item(cod="X", ncm="30049099", anvisa=""):
    return Item(num_item="1", cod_item=cod, descricao="", ncm=ncm, cfop="5102", quantidade=Decimal("1"),
                unidade="UN", vl_item=Decimal("100"), vl_desc=Z, vl_icms=Z, vl_icms_st=Z, vl_ipi=Z,
                vl_pis=Z, vl_cofins=Z, registro_anvisa=anvisa)


def _doc(cnpj="99"):
    return Documento(ind_oper="1", cod_mod="55", cod_sit="00", serie="1", numero="1", chave="", data="",
                     participante=Participante(cnpj=cnpj, nome=""))


def _motor(fake, produtos=None, compradores=None):
    return ClassificadorSaude(CalculadoraClient(fake.url), "2027-01-15", produtos or {}, compradores or {})


# --- leitor de NF-e -----------------------------------------------------------

def test_nfe_le_destinatario_itens_e_anvisa():
    parser = NfeParser(NFE)
    docs = list(parser.documentos())
    assert [d.numero for d in docs] == ["5001", "5002", "5003"]
    assert parser.empresa.cnpj == "11222333000181"
    hosp = docs[0]
    assert hosp.participante.cnpj == "33444555000166" and hosp.municipio_operacao == "3550308"
    dipirona, seringa = hosp.itens[0], hosp.itens[2]
    assert dipirona.registro_anvisa == "1000000000011" and dipirona.pmc == Decimal("120.00")
    assert dipirona.vl_icms == Decimal("153.00") and dipirona.vl_pis == Z  # PIS/COFINS monofásico (CST 04)
    assert seringa.vl_frete == Decimal("20.00") and seringa.vl_pis == Decimal("5.28")


# --- regras de medicamentos (decididas pelo cadastro) -------------------------

def test_medicamento_regras_por_comprador_e_cadastro():
    prod = {
        "M": Produto("M", TipoProduto.MEDICAMENTO, "1", False, ""),
        "Z": Produto("Z", TipoProduto.MEDICAMENTO, "2", True, ""),
        "V": Produto("V", TipoProduto.SORO_VACINA, "3", False, ""),
    }
    comp = {"11": TipoComprador.ORGAO_PUBLICO, "22": TipoComprador.ENTIDADE_IMUNE}
    with FakeCalculadora() as fake:
        m = _motor(fake, prod, comp)
        assert m.classificar_item(_item("M"), _doc()).cclasstrib == "200032"        # regra geral: 60%
        assert m.classificar_item(_item("Z"), _doc()).cclasstrib == "200009"        # lista de alíquota zero
        assert m.classificar_item(_item("V"), _doc()).cclasstrib == "200053"        # soro/vacina
        assert m.classificar_item(_item("M"), _doc("11")).cclasstrib == "200010"    # órgão público
        imune = m.classificar_item(_item("M"), _doc("22"))
        assert imune.cclasstrib == "200010" and "CONFIRMAR_CODIGO_ENTIDADE_IMUNE" in imune.alertas
        assert fake.consultas_ncm == []  # medicamentos não dependem do ncm-aplicavel


def test_medicamento_sem_anvisa_vai_para_integral_com_alerta():
    prod = {"M": Produto("M", TipoProduto.MEDICAMENTO, "", False, "")}
    with FakeCalculadora() as fake:
        c = _motor(fake, prod).classificar_item(_item("M"), _doc())
    assert (c.cst, c.cclasstrib) == ("000", "000001")
    assert "MEDICAMENTO_SEM_REGISTRO_ANVISA" in c.alertas


def test_sem_cadastro_mas_com_grupo_med_na_nfe_e_medicamento():
    with FakeCalculadora() as fake:
        c = _motor(fake).classificar_item(_item("?", anvisa="123"), _doc())
    assert c.cclasstrib == "200032" and "PRODUTO_SEM_CADASTRO" in c.alertas


# --- regras por NCM (validadas na calculadora) --------------------------------

def test_dispositivos_e_acessibilidade_pelo_ncm_aplicavel():
    prod = {"S": Produto("S", TipoProduto.DISPOSITIVO_MEDICO, "", False, ""),
            "C": Produto("C", TipoProduto.ACESSIBILIDADE, "", False, ""),
            "B": Produto("B", TipoProduto.DISPOSITIVO_MEDICO, "", False, "")}
    with FakeCalculadora() as fake:
        m = _motor(fake, prod, {"11": TipoComprador.ORGAO_PUBLICO})
        assert m.classificar_item(_item("S", "90183119"), _doc()).cclasstrib == "200030"  # Anexo IV, 60%
        assert m.classificar_item(_item("C", "87139000"), _doc()).cclasstrib == "200007"  # Anexo XIII, zero
        fora = m.classificar_item(_item("B", "39269090"), _doc())
        assert fora.cclasstrib == "000001" and "NCM_FORA_DOS_ANEXOS" in fora.alertas
        # testa alíquota zero (Anexo XII) antes da redução de 60% (Anexo IV)
        assert fake.consultas_ncm[:2] == [("200004", "90183119"), ("200030", "90183119")]


def test_sem_cadastro_tenta_anexos_pelo_ncm():
    with FakeCalculadora() as fake:
        c = _motor(fake).classificar_item(_item("SERINGA", "90183119"), _doc())
    assert c.cclasstrib == "200030"
    assert {"PRODUTO_SEM_CADASTRO", "CLASSIFICADO_SO_PELO_NCM"} <= set(c.alertas)


# --- ponta a ponta: distribuidora ---------------------------------------------

def test_distribuidora_ponta_a_ponta(tmp_path):
    with FakeCalculadora() as fake:
        cfg = tmp_path / "saude.toml"
        texto = CONFIG.read_text(encoding="utf-8").replace("http://localhost:8080/api", fake.url)
        texto = texto.replace("../exemplos/saude/", str(RAIZ / "exemplos" / "saude") + "/")
        cfg.write_text(texto, encoding="utf-8")
        saida = tmp_path / "out"
        assert main(["simular", str(NFE), "--config", str(cfg), "--saida", str(saida)]) == 0

    # IBS vai para o município do destinatário (envios em paralelo: a ordem varia)
    assert sorted(op["municipio"] for op in fake.recebidas) == [3509502, 3550308, 3552205]

    with (saida / "itens.csv").open(encoding="utf-8") as f:
        itens = list(csv.DictReader(f, delimiter=";"))
    assert len(itens) == 13
    with (saida / "pendencias.csv").open(encoding="utf-8") as f:
        pendencias = [(p["ncm"], p["estado"]) for p in csv.DictReader(f, delimiter=";")]
    assert pendencias == [("40151100", "NCM_INEXISTENTE")]
    def chave(nota, cod):
        return next(i for i in itens if i["cod_item"] == cod and f"{nota:09d}" in i["documento"])

    hosp, pref, sc = 5001, 5002, 5003
    assert chave(hosp, "MED001")["cclasstrib"] == "200032"
    assert chave(hosp, "MED002")["cclasstrib"] == "200009"
    assert chave(hosp, "VAC001")["cclasstrib"] == "200053"
    assert chave(hosp, "ACE001")["cclasstrib"] == "200007"
    assert chave(hosp, "MED003")["alertas"] == "MEDICAMENTO_SEM_REGISTRO_ANVISA"
    assert chave(hosp, "DIS001")["bc_cbs_ibs"] == "278.80"   # 320 + 20 de frete - 61,20 de ICMS
    assert chave(pref, "MED001")["cclasstrib"] == "200010"
    assert chave(pref, "DIS001")["cclasstrib"] == "200030"   # dublê não aceita a seringa no 200005
    assert chave(sc, "LUV001")["estado"] == "NCM_INEXISTENTE"
    assert chave(sc, "NUT001")["cclasstrib"] == "200033"

    with (saida / "revisao.csv").open(encoding="utf-8") as f:
        revisao = {(l[2], l[6]) for l in list(csv.reader(f, delimiter=";"))[1:]}
    assert revisao == {
        ("MED003", "MEDICAMENTO_SEM_REGISTRO_ANVISA"),
        ("MED001", "CONFIRMAR_CODIGO_ENTIDADE_IMUNE"),   # Santa Casa (CEBAS)
        ("VAC001", "CONFIRMAR_CODIGO_ENTIDADE_IMUNE"),
    }
