"""Governança da classificação: base legal e aprovação de cada regra.

- **Base legal:** vem da própria calculadora (`dados-abertos/fundamentacoes-legais`),
  pelo cClassTrib que a regra produziu. Assim o relatório cita o mesmo artigo que a
  Receita associa ao código, sem uma tabela paralela para manter.
- **Aprovação:** um CSV mantido pela área tributária diz quem aprovou cada regra e
  quando. A aprovação vale para o par (regra, cClassTrib): se a regra passar a
  produzir outro código, ela volta a ficar pendente de aprovação.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, replace
from pathlib import Path

import requests

from .classificacao import Classificacao
from .rtc.client import CalculadoraClient, CalculadoraIndisponivel

log = logging.getLogger(__name__)

REGRA_NAO_APROVADA = "REGRA_NAO_APROVADA"


@dataclass(frozen=True)
class Aprovacao:
    regra_id: str
    cclasstrib: str
    aprovado_por: str
    aprovado_em: str
    observacao: str = ""

    def __str__(self) -> str:
        return f"{self.aprovado_por} em {self.aprovado_em}"


def ler_aprovacoes(caminho: str | Path) -> dict[tuple[str, str], Aprovacao]:
    with Path(caminho).open(encoding="utf-8", newline="") as arq:
        linhas = csv.DictReader((l for l in arq if not l.startswith("#")), delimiter=";")
        aprovacoes = {}
        for l in linhas:
            a = Aprovacao(regra_id=l["regra_id"].strip(), cclasstrib=l["cclasstrib"].strip().zfill(6),
                          aprovado_por=l["aprovado_por"].strip(), aprovado_em=l["aprovado_em"].strip(),
                          observacao=(l.get("observacao") or "").strip())
            if not (a.regra_id and a.aprovado_por and a.aprovado_em):
                raise ValueError(f"{caminho}: aprovação incompleta (regra_id, aprovado_por e aprovado_em): {l}")
            aprovacoes[(a.regra_id, a.cclasstrib)] = a
    return aprovacoes


class Governanca:
    def __init__(self, cliente: CalculadoraClient, data: str,
                 aprovacoes: dict[tuple[str, str], Aprovacao] | None = None,
                 exigir_aprovacao: bool = False) -> None:
        self.cliente = cliente
        self.data = data
        self.aprovacoes = aprovacoes or {}
        self.exigir_aprovacao = exigir_aprovacao

    @classmethod
    def da_config(cls, cliente: CalculadoraClient, config) -> "Governanca":
        aprovacoes = ler_aprovacoes(config.aprovacoes_csv) if config.aprovacoes_csv else {}
        return cls(cliente, config.data_fato_gerador, aprovacoes, config.exigir_aprovacao)

    def _base_legal(self, cclasstrib: str) -> str:
        try:
            return self.cliente.fundamentacao(cclasstrib, self.data)
        except (requests.HTTPError, CalculadoraIndisponivel) as exc:
            log.warning("Sem base legal para %s: %s", cclasstrib, exc)
            return ""

    def aplicar(self, cls: Classificacao) -> Classificacao:
        aprovacao = self.aprovacoes.get((cls.id_regra, cls.cclasstrib))
        alertas = cls.alertas
        if aprovacao is None and self.exigir_aprovacao:
            alertas = (*alertas, REGRA_NAO_APROVADA)
        return replace(cls, base_legal=self._base_legal(cls.cclasstrib),
                       aprovacao=str(aprovacao) if aprovacao else "", alertas=alertas)
