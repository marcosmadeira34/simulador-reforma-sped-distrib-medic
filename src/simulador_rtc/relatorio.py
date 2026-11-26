"""Gravação dos resultados: itens, resumos, pendências, revisão e regras usadas."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .simulacao import CREDITO, LinhaResultado

DOIS = Decimal("0.01")


def _fmt(v: Decimal) -> str:
    return str(v.quantize(DOIS))


@dataclass
class Totais:
    itens: int = 0
    calculados: int = 0
    pendentes: int = 0
    em_revisao: int = 0
    carga_atual: Decimal = Decimal("0")   # tributos de todos os itens calculados (bruto)
    carga_nova: Decimal = Decimal("0")
    debito_atual: Decimal = Decimal("0")  # saídas
    debito_novo: Decimal = Decimal("0")
    credito_atual: Decimal = Decimal("0")  # entradas: parte recuperável
    credito_novo: Decimal = Decimal("0")
    por_estado: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def diferenca(self) -> Decimal:
        return self.carga_nova - self.carga_atual

    @property
    def saldo_atual(self) -> Decimal:
        """Tributo líquido a pagar hoje: débitos das saídas menos créditos das entradas."""
        return self.debito_atual - self.credito_atual

    @property
    def saldo_novo(self) -> Decimal:
        return self.debito_novo - self.credito_novo

    @property
    def diferenca_liquida(self) -> Decimal:
        return self.saldo_novo - self.saldo_atual


CAMPOS_ITENS = [
    "documento", "data", "ind_oper", "natureza", "participante", "num_item", "cod_item", "descricao", "ncm", "cfop", "registro_anvisa",
    "cst", "cclasstrib", "tratamento", "regra_classificacao", "regra_id", "base_legal", "aprovacao",
    "alertas", "estado", "vl_item",
    "icms", "icms_st", "ipi", "pis", "cofins", "carga_atual",
    "bc_cbs_ibs", "cbs", "ibs", "is", "remanescentes", "carga_nova", "diferenca",
    "credito_atual", "credito_novo", "erros",
]


def gravar(linhas: Iterable[LinhaResultado], pasta: str | Path) -> Totais:
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    totais = Totais()
    por_ncm: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal("0")] * 3)
    por_trat: dict[tuple[str, str], list[Decimal]] = defaultdict(lambda: [Decimal("0")] * 4)
    por_regra: dict[tuple[str, ...], list[Decimal]] = defaultdict(lambda: [Decimal("0")] * 2)

    with (pasta / "itens.csv").open("w", encoding="utf-8", newline="") as f_itens, \
         (pasta / "pendencias.csv").open("w", encoding="utf-8", newline="") as f_pend, \
         (pasta / "revisao.csv").open("w", encoding="utf-8", newline="") as f_rev:
        w_itens = csv.DictWriter(f_itens, fieldnames=CAMPOS_ITENS, delimiter=";")
        w_pend = csv.writer(f_pend, delimiter=";")
        w_itens.writeheader()
        w_pend.writerow(["documento", "num_item", "ncm", "cfop", "estado", "erros"])
        w_rev = csv.writer(f_rev, delimiter=";")
        w_rev.writerow(["documento", "num_item", "cod_item", "descricao", "ncm", "cclasstrib", "alertas"])

        for l in linhas:
            it, cls = l.item, l.classificacao
            totais.itens += 1
            totais.por_estado[l.estado] += 1
            calculado = l.estado in ("CALCULADO", "CALCULO_SIMULADO")
            w_itens.writerow({
                "documento": l.documento, "data": l.data, "ind_oper": l.ind_oper, "natureza": l.natureza,
                "participante": l.participante,
                "num_item": it.num_item, "cod_item": it.cod_item, "descricao": it.descricao,
                "ncm": it.ncm, "cfop": it.cfop, "registro_anvisa": it.registro_anvisa,
                "cst": cls.cst if cls else "", "cclasstrib": cls.cclasstrib if cls else "",
                "tratamento": cls.descricao if cls else "",
                "regra_classificacao": cls.regra if cls else "", "regra_id": cls.id_regra if cls else "",
                "base_legal": cls.base_legal if cls else "", "aprovacao": cls.aprovacao if cls else "",
                "alertas": ",".join(l.alertas),
                "estado": l.estado,
                "vl_item": _fmt(it.vl_item), "icms": _fmt(it.vl_icms), "icms_st": _fmt(it.vl_icms_st),
                "ipi": _fmt(it.vl_ipi), "pis": _fmt(it.vl_pis), "cofins": _fmt(it.vl_cofins),
                "carga_atual": _fmt(l.carga_atual),
                "bc_cbs_ibs": _fmt(l.v_bc_nova), "cbs": _fmt(l.v_cbs), "ibs": _fmt(l.v_ibs),
                "is": _fmt(l.v_is), "remanescentes": _fmt(l.remanescentes), "carga_nova": _fmt(l.carga_nova) if calculado else "",
                "diferenca": _fmt(l.diferenca) if calculado else "",
                "credito_atual": _fmt(l.credito_atual) if calculado else "",
                "credito_novo": _fmt(l.credito_novo) if calculado else "",
                "erros": " | ".join(l.erros),
            })
            if cls is not None:
                r = por_regra[(cls.id_regra, cls.cst, cls.cclasstrib, cls.descricao, cls.base_legal, cls.aprovacao)]
                r[0] += 1
                r[1] += it.vl_item
            if l.alertas:
                totais.em_revisao += 1
                w_rev.writerow([l.documento, it.num_item, it.cod_item, it.descricao, it.ncm,
                                cls.cclasstrib if cls else "", ",".join(l.alertas)])
            if calculado:
                totais.calculados += 1
                totais.carga_atual += l.carga_atual
                totais.carga_nova += l.carga_nova
                if l.natureza == CREDITO:
                    totais.credito_atual += l.credito_atual
                    totais.credito_novo += l.credito_novo
                else:
                    totais.debito_atual += l.carga_atual
                    totais.debito_novo += l.carga_nova
                acc = por_ncm[it.ncm]
                acc[0] += it.vl_item
                acc[1] += l.carga_atual
                acc[2] += l.carga_nova
                t = por_trat[(cls.cclasstrib, cls.descricao)]
                t[0] += 1
                t[1] += it.vl_item
                t[2] += l.carga_atual
                t[3] += l.carga_nova
            else:
                totais.pendentes += 1
                w_pend.writerow([l.documento, it.num_item, it.ncm, it.cfop, l.estado, " | ".join(l.erros)])

    with (pasta / "resumo_ncm.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["ncm", "valor_itens", "carga_atual", "carga_nova", "diferenca", "carga_atual_%", "carga_nova_%"])
        for ncm, (valor, atual, nova) in sorted(por_ncm.items(), key=lambda kv: kv[1][2] - kv[1][1]):
            pct = lambda x: _fmt(x / valor * 100) if valor else ""  # noqa: E731
            w.writerow([ncm, _fmt(valor), _fmt(atual), _fmt(nova), _fmt(nova - atual), pct(atual), pct(nova)])

    with (pasta / "resumo_tratamento.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["cclasstrib", "tratamento", "itens", "valor_itens", "carga_atual", "carga_nova", "diferenca"])
        for (cc, desc), (n, valor, atual, nova) in sorted(por_trat.items(), key=lambda kv: -kv[1][1]):
            w.writerow([cc, desc, int(n), _fmt(valor), _fmt(atual), _fmt(nova), _fmt(nova - atual)])

    # Trilha de auditoria: cada regra usada, com a base legal e quem a aprovou.
    with (pasta / "regras_usadas.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["regra_id", "cst", "cclasstrib", "tratamento", "base_legal", "aprovacao", "itens", "valor_itens"])
        for chave, (n, valor) in sorted(por_regra.items()):
            w.writerow([*chave, int(n), _fmt(valor)])
    return totais
