"""Motor de classificação do setor de medicamentos e produtos hospitalares.

Duas lógicas, conforme o que a Calculadora RTC consegue validar:

- **Medicamentos:** a calculadora aceita qualquer NCM nos cClassTrib de medicamentos.
  Quem decide é o cadastro: registro na Anvisa, se o produto está na lista de
  alíquota zero, se é soro/vacina, e o tipo de comprador.
- **Dispositivos médicos, acessibilidade e nutrição:** são listas de NCM (Anexos IV,
  V, VI, XII e XIII). A calculadora valida pelo endpoint `ncm-aplicavel`, e o motor
  fica com a primeira classificação aceita, na ordem de prioridade.

Códigos conferidos em `GET .../classificacoes-tributarias/cbs-ibs/200?data=2027-01-15`
na calculadora 1.6.1. Revise a cada nova versão da calculadora.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..classificacao import INTEGRAL, Classificacao
from ..modelos import Documento, Item
from ..rtc.client import CalculadoraClient


class TipoProduto(str, Enum):
    MEDICAMENTO = "MEDICAMENTO"
    SORO_VACINA = "SORO_VACINA"
    DISPOSITIVO_MEDICO = "DISPOSITIVO_MEDICO"
    ACESSIBILIDADE = "ACESSIBILIDADE"
    NUTRICAO = "NUTRICAO"
    OUTRO = "OUTRO"


class TipoComprador(str, Enum):
    ORGAO_PUBLICO = "ORGAO_PUBLICO"
    ENTIDADE_IMUNE = "ENTIDADE_IMUNE"  # entidade de saúde imune (CEBAS)
    PRIVADO = "PRIVADO"


# Classificações de saúde (CST 200). Descrições da tabela oficial da calculadora.
CLASSES = {
    "200004": "Dispositivos médicos (Anexo XII) - alíquota zero",
    "200005": "Dispositivos médicos do Anexo IV para órgão público ou entidade de saúde imune - alíquota zero",
    "200007": "Dispositivos de acessibilidade (Anexo XIII) - alíquota zero",
    "200008": "Dispositivos de acessibilidade do Anexo V para órgão público - alíquota zero",
    "200009": "Medicamentos registrados na Anvisa - alíquota zero",
    "200010": "Medicamentos registrados na Anvisa adquiridos por órgão público - alíquota zero",
    "200011": "Nutrição enteral e parenteral (Anexo VI) para órgão público - alíquota zero",
    "200030": "Dispositivos médicos (Anexo IV) - redução de 60%",
    "200031": "Dispositivos de acessibilidade (Anexo V) - redução de 60%",
    "200032": "Medicamentos registrados na Anvisa ou manipulados - redução de 60%",
    "200033": "Nutrição enteral e parenteral (Anexo VI) - redução de 60%",
    "200053": "Soros e vacinas registrados na Anvisa - alíquota zero",
}

# Ordem de teste no ncm-aplicavel: alíquota zero antes de redução de 60%.
CANDIDATOS_NCM = {
    TipoProduto.DISPOSITIVO_MEDICO: ["200004", "200005", "200030"],
    TipoProduto.ACESSIBILIDADE: ["200007", "200008", "200031"],
    TipoProduto.NUTRICAO: ["200011", "200033"],
}

# Classificações que só valem para certos compradores (descrições da tabela oficial).
EXIGE_COMPRADOR = {
    "200005": {"ORGAO_PUBLICO", "ENTIDADE_IMUNE"},  # dispositivos Anexo IV: órgão público ou entidade imune
    "200008": {"ORGAO_PUBLICO"},                    # acessibilidade Anexo V: órgão público
    "200011": {"ORGAO_PUBLICO"},                    # nutrição Anexo VI: órgão público
}


@dataclass(frozen=True)
class Produto:
    cod_item: str
    tipo: TipoProduto
    registro_anvisa: str
    aliquota_zero: bool  # está na lista de medicamentos com alíquota zero (confirmar na LC 214)
    descricao: str


def _ler_csv(caminho: str | Path) -> list[dict[str, str]]:
    with Path(caminho).open(encoding="utf-8", newline="") as arq:
        return list(csv.DictReader((l for l in arq if not l.startswith("#")), delimiter=";"))


def _digitos(valor: str) -> str:
    return "".join(c for c in valor if c.isdigit())


_cnpj = _digitos


class ClassificadorSaude:
    def __init__(self, cliente: CalculadoraClient, data_fato_gerador: str,
                 produtos: dict[str, Produto], compradores: dict[str, TipoComprador]) -> None:
        self.cliente = cliente
        self.data = data_fato_gerador
        self.produtos = produtos
        self.compradores = compradores

    @classmethod
    def de_csv(cls, cliente: CalculadoraClient, data_fato_gerador: str,
               produtos_csv: str | Path, participantes_csv: str | Path) -> "ClassificadorSaude":
        produtos = {
            l["cod_item"].strip(): Produto(
                cod_item=l["cod_item"].strip(), tipo=TipoProduto(l["tipo"].strip().upper()),
                registro_anvisa=l.get("registro_anvisa", "").strip(),
                aliquota_zero=l.get("aliquota_zero", "").strip().upper() in ("S", "SIM", "TRUE", "1"),
                descricao=l.get("descricao", "").strip(),
            )
            for l in _ler_csv(produtos_csv)
        }
        compradores = {_cnpj(l["cnpj"]): TipoComprador(l["tipo"].strip().upper()) for l in _ler_csv(participantes_csv)}
        return cls(cliente, data_fato_gerador, produtos, compradores)

    # -- interface usada pelo Simulador -------------------------------------
    def classificar_item(self, item: Item, doc: Documento) -> Classificacao:
        produto = self.produtos.get(item.cod_item)
        alertas: list[str] = []
        if produto is None:
            alertas.append("PRODUTO_SEM_CADASTRO")
        tipo = produto.tipo if produto else (TipoProduto.MEDICAMENTO if item.registro_anvisa else None)
        comprador = self._comprador(doc)

        if tipo in (TipoProduto.MEDICAMENTO, TipoProduto.SORO_VACINA):
            return self._medicamento(item, produto, tipo, comprador, alertas)
        if tipo is TipoProduto.OUTRO:
            return self._integral("Produto fora dos tratamentos de saúde", alertas, "fora-saude")

        if tipo is None:  # sem cadastro: tenta todos os anexos por NCM
            candidatos = [c for lista in CANDIDATOS_NCM.values() for c in lista]
            alertas.append("CLASSIFICADO_SO_PELO_NCM")
        else:
            candidatos = CANDIDATOS_NCM[tipo]
        candidatos = [c for c in candidatos
                      if c not in EXIGE_COMPRADOR or (comprador and comprador.value in EXIGE_COMPRADOR[c])]

        for cclass in candidatos:
            if self.cliente.ncm_aplicavel(cclass, item.ncm, self.data):
                return self._classe(cclass, f"ncm {item.ncm} aceito em {cclass}", alertas, "ncm-anexo")

        if tipo is not None:
            alertas.append("NCM_FORA_DOS_ANEXOS")
        return self._integral("Nenhum anexo de saúde aceitou o NCM", alertas, "ncm-fora-anexos")

    # -- regras -------------------------------------------------------------
    def _medicamento(self, item: Item, produto: Produto | None, tipo: TipoProduto,
                     comprador: TipoComprador | None, alertas: list[str]) -> Classificacao:
        anvisa = item.registro_anvisa or (produto.registro_anvisa if produto else "")
        if not anvisa:
            # Sem registro na Anvisa (nem manipulado), não há benefício: risco de enquadramento.
            alertas.append("MEDICAMENTO_SEM_REGISTRO_ANVISA")
            return self._integral("Medicamento sem registro na Anvisa", alertas, "med-sem-anvisa")
        if produto and item.registro_anvisa and produto.registro_anvisa \
                and _digitos(item.registro_anvisa) != _digitos(produto.registro_anvisa):
            alertas.append("REGISTRO_ANVISA_DIVERGENTE_DO_CADASTRO")

        if comprador is TipoComprador.ORGAO_PUBLICO:
            return self._classe("200010", "medicamento para órgão público", alertas, "med-orgao-publico")
        if comprador is TipoComprador.ENTIDADE_IMUNE:
            # LC 214, art. 146, §1º, II prevê alíquota zero para entidades CEBAS, mas a
            # descrição do 200010 na calculadora cita só órgãos públicos.
            alertas.append("CONFIRMAR_CODIGO_ENTIDADE_IMUNE")
            return self._classe("200010", "medicamento para entidade de saúde imune", alertas, "med-entidade-imune")
        if tipo is TipoProduto.SORO_VACINA:
            return self._classe("200053", "soro ou vacina", alertas, "soro-vacina")
        if produto and produto.aliquota_zero:
            return self._classe("200009", "medicamento na lista de alíquota zero (cadastro)", alertas, "med-aliquota-zero")
        return self._classe("200032", "medicamento com registro na Anvisa", alertas, "med-anvisa")

    def _comprador(self, doc: Documento) -> TipoComprador | None:
        if doc.ind_oper != "1" or doc.participante is None:
            return None  # entradas: quem compra é a própria empresa
        return self.compradores.get(_cnpj(doc.participante.cnpj), TipoComprador.PRIVADO)

    @staticmethod
    def _classe(cclass: str, motivo: str, alertas: list[str], regra_id: str) -> Classificacao:
        return Classificacao(cst="200", cclasstrib=cclass, descricao=CLASSES[cclass],
                             regra=f"saude: {motivo}", alertas=tuple(alertas), regra_id=f"saude:{regra_id}")

    @staticmethod
    def _integral(motivo: str, alertas: list[str], regra_id: str) -> Classificacao:
        cst, cclass = INTEGRAL
        return Classificacao(cst=cst, cclasstrib=cclass, descricao="Tributação integral",
                             regra=f"saude: {motivo}", alertas=tuple(alertas), regra_id=f"saude:{regra_id}")
