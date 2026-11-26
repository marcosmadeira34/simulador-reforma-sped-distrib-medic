"""De-para de NCM/CFOP para CST e cClassTrib da CBS/IBS.

As regras ficam num CSV (`config/classificacao.csv`) para que a área fiscal
revise e mantenha sem mexer em código. Vale a regra com o prefixo de NCM mais
longo; o CFOP pode ser exato ou `*`. As colunas `is_*` (opcionais) ligam o
Imposto Seletivo para o item.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class ClassificacaoIS:
    cst: str
    cclasstrib: str
    ad_valorem: Decimal | None  # alíquota nominal (%) usada na simulação


@dataclass(frozen=True)
class Classificacao:
    cst: str
    cclasstrib: str
    descricao: str
    regra: str  # qual regra casou, para rastreabilidade
    imposto_seletivo: ClassificacaoIS | None = None
    alertas: tuple[str, ...] = ()  # motivos para revisão humana, mesmo com cálculo feito
    regra_id: str = ""     # identificador estável da regra, usado nas aprovações
    base_legal: str = ""   # preenchido pela governança (fundamentação da calculadora)
    aprovacao: str = ""    # preenchido pela governança: quem aprovou e quando

    @property
    def id_regra(self) -> str:
        return self.regra_id or self.regra


INTEGRAL = ("000", "000001")


@dataclass(frozen=True)
class _Regra:
    ncm_prefixo: str
    cfop: str
    cst: str
    cclasstrib: str
    descricao: str
    imposto_seletivo: ClassificacaoIS | None = None


def _is(linha: dict[str, str | None]) -> ClassificacaoIS | None:
    cst = (linha.get("is_cst") or "").strip()
    if not cst:
        return None
    aliq = (linha.get("is_ad_valorem") or "").strip()
    return ClassificacaoIS(cst=cst.zfill(3), cclasstrib=(linha.get("is_cclasstrib") or "").strip().zfill(6),
                           ad_valorem=Decimal(aliq) if aliq else None)


class Classificador:
    def __init__(self, regras: list[_Regra]) -> None:
        # Mais específicas primeiro: prefixo maior e CFOP exato antes de '*'.
        self.regras = sorted(regras, key=lambda r: (len(r.ncm_prefixo), r.cfop != "*"), reverse=True)

    @classmethod
    def de_csv(cls, caminho: str | Path) -> "Classificador":
        with Path(caminho).open(encoding="utf-8", newline="") as arq:
            linhas = csv.DictReader((l for l in arq if not l.startswith("#")), delimiter=";")
            regras = [
                _Regra(
                    ncm_prefixo=l["ncm_prefixo"].strip(),
                    cfop=(l["cfop"].strip() or "*"),
                    cst=l["cst"].strip().zfill(3),
                    cclasstrib=l["cclasstrib"].strip().zfill(6),
                    descricao=l["descricao"].strip(),
                    imposto_seletivo=_is(l),
                )
                for l in linhas
            ]
        return cls(regras)

    def classificar_item(self, item, doc) -> Classificacao | None:
        """Interface comum aos classificadores (genérico e setoriais)."""
        return self.classificar(item.ncm, item.cfop)

    def classificar(self, ncm: str, cfop: str) -> Classificacao | None:
        for r in self.regras:
            if ncm.startswith(r.ncm_prefixo) and r.cfop in ("*", cfop):
                return Classificacao(
                    cst=r.cst, cclasstrib=r.cclasstrib, descricao=r.descricao,
                    regra=f"ncm^{r.ncm_prefixo or '*'}|cfop={r.cfop}",
                    imposto_seletivo=r.imposto_seletivo,
                )
        return None
