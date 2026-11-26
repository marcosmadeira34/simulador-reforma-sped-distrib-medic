"""Handlers da Step Function, sem AWS: pasta local no lugar do S3 e o dublê da calculadora."""

import csv
import json
import re
import shutil
from pathlib import Path

import pytest

from simulador_rtc.aws import handlers
from simulador_rtc.cli import VersaoDivergente
from simulador_rtc.rtc.client import CalculadoraIndisponivel

from fake_calculadora import FakeCalculadora

RAIZ = Path(__file__).resolve().parents[1]
ASL = RAIZ / "infra" / "state_machine.asl.json"
PREFIXO = "entradas/demo"


def _evento(config="config/saude_transicao.toml"):
    return {"prefixo": PREFIXO, "config": config,
            "entradas": ["exemplos/saude/efd_icms_ipi_outubro.txt", "exemplos/saude/nfe"]}


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    """Simula o bucket: entradas/demo/{config,exemplos/saude} como o infra/demo.py envia."""
    ignorar = shutil.ignore_patterns("*Zone.Identifier")
    shutil.copytree(RAIZ / "config", tmp_path / PREFIXO / "config", ignore=ignorar)
    shutil.copytree(RAIZ / "exemplos" / "saude", tmp_path / PREFIXO / "exemplos" / "saude", ignore=ignorar)
    monkeypatch.setenv("ARMAZENAMENTO_LOCAL", str(tmp_path))
    return tmp_path


def test_fluxo_completo_como_a_step_function(bucket, monkeypatch):
    with FakeCalculadora() as fake:
        monkeypatch.setenv("RTC_URL", fake.url)
        entrada = _evento()

        # ConferirCalculadora
        conferido = handlers.conferir(entrada)
        nomes = [c["nome"] for c in conferido["cenarios"]]
        assert nomes == [str(a) for a in range(2026, 2034)]
        assert conferido["registro"]["calculadora"]["versaoApp"] == "1.6.1"
        assert conferido["registro"]["entradas"][0]["caminho"] == entrada["entradas"][0]  # sem /tmp
        json.dumps(conferido)  # precisa ser serializável para trafegar entre estados

        # SimularCenarios (Map): o ItemSelector monta este evento para cada cenário
        resultados = [handlers.simular_cenario({**entrada, "registro": conferido["registro"],
                                                "execucao": "exec-1", "cenario": c})
                      for c in conferido["cenarios"]]

    assert all((bucket / "resultados/exec-1" / n / "itens.csv").exists() for n in nomes)
    assert all(len(json.dumps(r)) < 256 * 1024 for r in resultados)  # limite de payload

    # Consolidar
    consolidado = handlers.consolidar({"execucao": "exec-1", "resultados": resultados})
    with (bucket / "resultados/exec-1/cenarios.csv").open(encoding="utf-8") as f:
        linhas = list(csv.DictReader(f, delimiter=";"))
    assert [l["cenario"] for l in linhas] == nomes
    assert consolidado["em_revisao"] == sum(r["totais"]["em_revisao"] for r in resultados) > 0


def test_conferir_sem_calculadora_levanta_erro_que_a_step_function_repete(bucket, monkeypatch):
    monkeypatch.setenv("RTC_URL", "http://127.0.0.1:9/api")
    with pytest.raises(CalculadoraIndisponivel):
        handlers.conferir(_evento())


def test_conferir_versao_divergente(bucket, monkeypatch):
    with FakeCalculadora(versao="9.9.9") as fake:
        monkeypatch.setenv("RTC_URL", fake.url)
        with pytest.raises(VersaoDivergente):
            handlers.conferir(_evento())


def test_config_de_cenario_unico(bucket, monkeypatch):
    with FakeCalculadora() as fake:
        monkeypatch.setenv("RTC_URL", fake.url)
        conferido = handlers.conferir(_evento("config/saude.toml"))
    assert conferido["cenarios"] == [{"indice": 0, "nome": "unico"}]


# --- definição da máquina de estados ----------------------------------------------------------

def _estados(states: dict):
    for nome, estado in states.items():
        yield nome, estado, states
        if "ItemProcessor" in estado:
            yield from _estados(estado["ItemProcessor"]["States"])


def test_asl_transicoes_apontam_para_estados_existentes():
    asl = json.loads(ASL.read_text(encoding="utf-8"))
    assert asl["StartAt"] in asl["States"]
    for nome, estado, escopo in _estados(asl["States"]):
        destinos = [estado.get("Next"), estado.get("Default"),
                    *(c["Next"] for c in estado.get("Choices", [])),
                    *(c["Next"] for c in estado.get("Catch", []))]
        for d in filter(None, destinos):
            assert d in escopo, f"{nome} aponta para {d}, que não existe"
        if estado["Type"] not in ("Choice", "Succeed", "Fail"):
            assert estado.get("Next") or estado.get("End"), f"{nome} não tem Next nem End"


def test_asl_toda_etapa_notifica_a_falha_no_slack():
    """Task/Map do nível principal: depois do Retry, qualquer erro cai em NotificarFalha
    (exceto a reprovação, que é decisão de negócio). States.ALL tem de vir sozinho e por último."""
    asl = json.loads(ASL.read_text(encoding="utf-8"))
    for nome, estado in asl["States"].items():
        if estado["Type"] not in ("Task", "Map") or nome == "NotificarFalha":
            continue
        catch = estado.get("Catch", [])
        assert catch and catch[-1]["ErrorEquals"] == ["States.ALL"], f"{nome} sem Catch States.ALL no fim"
        assert catch[-1]["Next"] == "NotificarFalha"
        assert "falha" in catch[-1]["Assign"], f"{nome} não guarda $falha para a notificação"
        assert all("States.ALL" not in c["ErrorEquals"] for c in catch[:-1])
    assert asl["States"]["NotificarFalha"]["Next"] == "Falhou"
    assert asl["States"]["NotificarFalha"]["Catch"][0]["Next"] == "Falhou"  # aviso falhou: erro original


def test_asl_usa_os_nomes_de_erro_dos_handlers_e_as_substituicoes_do_stack():
    texto = ASL.read_text(encoding="utf-8")
    assert CalculadoraIndisponivel.__name__ in texto and VersaoDivergente.__name__ in texto
    stack = (RAIZ / "infra" / "stack.py").read_text(encoding="utf-8")
    for chave in set(re.findall(r"\$\{(\w+)\}", texto)):
        assert f'"{chave}"' in stack, f"${{{chave}}} não é substituído em infra/stack.py"
    for handler in re.findall(r'handlers\.\{handler\}|funcao\("\w+", "(\w+)"', stack):
        if handler:
            assert callable(getattr(handlers, handler))
