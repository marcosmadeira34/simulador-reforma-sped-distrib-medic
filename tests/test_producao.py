"""Robustez e análise: retentativas, versão, notas sem itens, créditos, cenários e governança."""

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from simulador_rtc.classificacao import Classificacao
from simulador_rtc.cli import main
from simulador_rtc.config import Config, carregar_cenarios
from simulador_rtc.governanca import REGRA_NAO_APROVADA, Governanca, ler_aprovacoes
from simulador_rtc.rtc.client import CalculadoraClient, CalculadoraIndisponivel
from simulador_rtc.sped.parser import EfdIcmsIpiParser

from fake_calculadora import FakeCalculadora

RAIZ = Path(__file__).resolve().parents[1]
SAUDE = RAIZ / "exemplos" / "saude"
EFD = SAUDE / "efd_icms_ipi_outubro.txt"
NFE = SAUDE / "nfe"
NOTA_SEM_XML = "35261011222333000181550010000050041000050040"


def _config(tmp_path, fake, nome="saude.toml", extra=""):
    texto = (RAIZ / "config" / nome).read_text(encoding="utf-8")
    texto = texto.replace("http://localhost:8080/api", fake.url)
    texto = texto.replace("../exemplos/saude/", str(SAUDE) + "/")
    cfg = tmp_path / nome
    cfg.write_text(texto + extra, encoding="utf-8")
    return cfg


def _itens(pasta):
    with (pasta / "itens.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


# --- retentativas -------------------------------------------------------------

def test_consulta_repete_falha_transitoria():
    esperas = []
    with FakeCalculadora() as fake:
        fake.falhas_restantes = 2
        cliente = CalculadoraClient(fake.url, dormir=esperas.append)
        assert cliente.ncm_existe("30024121", "2027-01-15")
    assert esperas == [0.5, 1.0] and len(fake.requisicoes) == 3


def test_consulta_desiste_depois_das_tentativas():
    with FakeCalculadora() as fake:
        fake.falhas_restantes = 10
        with pytest.raises(CalculadoraIndisponivel):
            CalculadoraClient(fake.url, tentativas=3, dormir=lambda s: None).ncm_aplicavel(
                "200030", "90183119", "2027-01-15")
    assert len(fake.requisicoes) == 3


def test_404_do_ncm_nao_e_repetido_e_fica_em_cache():
    with FakeCalculadora() as fake:
        cliente = CalculadoraClient(fake.url, dormir=lambda s: None)
        assert not cliente.ncm_existe("40151100", "2027-01-15")
        assert not cliente.ncm_existe("40151100", "2027-01-15")
    assert len(fake.requisicoes) == 1


# --- versão fixada ------------------------------------------------------------

def test_simulacao_para_se_a_calculadora_mudou_de_versao(tmp_path, capsys):
    with FakeCalculadora(versao="1.7.1") as fake:
        cfg = _config(tmp_path, fake)
        assert main(["simular", str(NFE), "--config", str(cfg), "--saida", str(tmp_path / "out")]) == 3
    assert "1.7.1" in capsys.readouterr().err and not (tmp_path / "out").exists()


# --- EFD: notas sem C170 ------------------------------------------------------

def test_efd_registra_notas_sem_itens():
    parser = EfdIcmsIpiParser(EFD)
    assert [d.numero for d in parser.documentos()] == ["7001", "7002"]  # só as compras têm C170
    assert len(parser.info.documentos_sem_itens) == 4                  # as 4 vendas (só C190)
    assert parser.info.documentos_sem_itens[-1] == NOTA_SEM_XML


# --- créditos: EFD de entradas + NF-e de saída ---------------------------------

def test_entradas_geram_credito_e_saldo_liquido(tmp_path):
    with FakeCalculadora() as fake:
        cfg = _config(tmp_path, fake)
        saida = tmp_path / "out"
        assert main(["simular", str(EFD), str(NFE), "--config", str(cfg), "--saida", str(saida)]) == 0

    itens = _itens(saida)
    assert len(itens) == 17  # 4 compras + 13 vendas
    entrada = {i["cod_item"]: i for i in itens if i["natureza"] == "CREDITO"}
    assert set(entrada) == {"MED001", "VAC001", "DIS001", "ACE001"}
    # seringa: crédito hoje = ICMS + PIS + COFINS; no cenário = ICMS + CBS (14,43) + IBS (0,16)
    assert entrada["DIS001"]["credito_atual"] == "136.25"
    assert entrada["DIS001"]["credito_novo"] == "104.59"
    assert entrada["VAC001"]["credito_novo"] == "0.00"  # 200053: adquirente não credita

    # a venda 5004 está na EFD só com C190 e não tem XML: fica de fora, mas registrada
    with (saida / "notas_sem_itens.csv").open(encoding="utf-8") as f:
        assert [l["documento"] for l in csv.DictReader(f, delimiter=";")] == [NOTA_SEM_XML]

    execucao = json.loads((saida / "execucao.json").read_text(encoding="utf-8"))
    assert execucao["calculadora"]["versaoApp"] == "1.6.1"
    assert len(execucao["entradas"]) == 2 and all(len(e["sha256"]) == 64 for e in execucao["entradas"])
    t = execucao["totais"]
    assert t["credito_atual"] == "352.25" and t["notas_sem_itens_nao_cobertas"] == 1
    assert Decimal(t["saldo_novo"]) == Decimal(t["debito_novo"]) - Decimal(t["credito_novo"])


# --- cenários da transição ----------------------------------------------------

def test_transicao_roda_um_cenario_por_ano(tmp_path):
    with FakeCalculadora() as fake:
        cfg = _config(tmp_path, fake, "saude_transicao.toml")
        saida = tmp_path / "out"
        assert main(["simular", str(EFD), str(NFE), "--config", str(cfg), "--saida", str(saida)]) == 0

    with (saida / "cenarios.csv").open(encoding="utf-8") as f:
        cenarios = {l["cenario"]: l for l in csv.DictReader(f, delimiter=";")}
    assert list(cenarios) == [str(a) for a in range(2026, 2034)]
    assert cenarios["2026"]["diferenca_liquida"] == "0.00"  # CBS/IBS de teste fora da carga
    assert {i["estado"] for i in _itens(saida / "2026") if i["cod_item"] == "MED001"} == {"CALCULADO"}

    # 2029: ICMS a 90%, tanto no remanescente quanto no ICMS abatido da base
    med_2029 = next(i for i in _itens(saida / "2029") if i["cod_item"] == "MED001" and i["natureza"] == "CREDITO")
    assert med_2029["remanescentes"] == "194.40"
    op = next(o for o in fake.recebidas if o["dhFatoGerador"].startswith("2029") and o["itens"][0]["ncm"] == "30049099"
              and o["itens"][0]["gComponentesBC"]["vProd"] == 1200)
    assert op["itens"][0]["gComponentesBC"]["vICMS"] == 194.4

    # 2033: sem ICMS, carga nova só com CBS/IBS
    assert all(i["remanescentes"] == "0.00" for i in _itens(saida / "2033") if i["estado"] == "CALCULO_SIMULADO")


def test_cenarios_exigem_nome_unico(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[calculadora]\nurl="http://x"\n[simulacao]\n'
                   '[[cenarios]]\nnome="a"\ndh_fato_gerador="2026-01-01T00:00:00-03:00"\n'
                   '[[cenarios]]\nnome="a"\ndh_fato_gerador="2026-02-01T00:00:00-03:00"\n')
    with pytest.raises(ValueError, match="nome"):
        carregar_cenarios(cfg)


def test_remanescentes_aceitam_fracao(tmp_path):
    base = '[calculadora]\nurl="http://x"\n[simulacao]\ndh_fato_gerador="2026-01-01T00:00:00-03:00"\n'
    cfg = tmp_path / "c.toml"
    cfg.write_text(base + "tributos_remanescentes = { ICMS = 0.9 }\n")
    c = Config.de_toml(cfg)
    assert c.fator("ICMS") == Decimal("0.9") and c.fator("PIS") == 0
    cfg.write_text(base + "tributos_remanescentes = { ICMS = 1.5 }\n")
    with pytest.raises(ValueError):
        Config.de_toml(cfg)


# --- governança ---------------------------------------------------------------

def _cls(regra_id, cclass):
    return Classificacao(cst="200", cclasstrib=cclass, descricao="", regra="", regra_id=regra_id)


def test_governanca_base_legal_e_aprovacao_por_regra_e_codigo():
    aprovacoes = ler_aprovacoes(SAUDE / "aprovacoes.csv")
    with FakeCalculadora() as fake:
        g = Governanca(CalculadoraClient(fake.url), "2027-01-15", aprovacoes, exigir_aprovacao=True)
        aprovada = g.aplicar(_cls("saude:med-anvisa", "200032"))
        outro_codigo = g.aplicar(_cls("saude:med-anvisa", "200009"))  # aprovação era para 200032
    assert aprovada.base_legal == "LC 214/2025, Art. 133"
    assert aprovada.aprovacao == "Área tributária (EXEMPLO) em 2026-11-10"
    assert REGRA_NAO_APROVADA not in aprovada.alertas
    assert outro_codigo.aprovacao == "" and REGRA_NAO_APROVADA in outro_codigo.alertas


def test_exigir_aprovacao_manda_regras_pendentes_para_revisao(tmp_path):
    with FakeCalculadora() as fake:
        cfg = _config(tmp_path, fake)
        cfg.write_text(cfg.read_text(encoding="utf-8").replace("exigir_aprovacao = false",
                                                               "exigir_aprovacao = true"), encoding="utf-8")
        saida = tmp_path / "out"
        assert main(["simular", str(NFE), "--config", str(cfg), "--saida", str(saida)]) == 0

    with (saida / "regras_usadas.csv").open(encoding="utf-8") as f:
        regras = {(l["regra_id"], l["cclasstrib"]): l for l in csv.DictReader(f, delimiter=";")}
    assert regras[("saude:soro-vacina", "200053")]["base_legal"] == "LC 214/2025, Art. 146, § 1º, III"
    assert regras[("saude:soro-vacina", "200053")]["aprovacao"].startswith("Área tributária")
    pendentes = {i["regra_id"] for i in _itens(saida) if REGRA_NAO_APROVADA in i["alertas"]}
    assert "saude:med-aliquota-zero" in pendentes and "saude:med-anvisa" not in pendentes
