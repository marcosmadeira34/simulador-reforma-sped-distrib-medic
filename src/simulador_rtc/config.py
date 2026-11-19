"""Configuração da simulação (lida de um arquivo TOML)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Aliquotas:
    cbs: Decimal
    ibs_estadual: Decimal
    ibs_municipal: Decimal


@dataclass(frozen=True)
class Config:
    url_calculadora: str
    timeout_s: float
    workers: int
    dh_fato_gerador: datetime
    aliquotas: Aliquotas | None
    classificacao_csv: Path
    informar_tp_doc: bool
    tributos_remanescentes: frozenset[str] = frozenset()
    setor: str = "generico"
    cadastro_produtos: Path | None = None
    cadastro_participantes: Path | None = None
    validar_ncm: bool = True

    @classmethod
    def de_toml(cls, caminho: str | Path) -> "Config":
        caminho = Path(caminho)
        dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
        calc, sim = dados["calculadora"], dados["simulacao"]
        dh = datetime.fromisoformat(sim["dh_fato_gerador"])
        aliq = sim.get("aliquotas_nominais")
        # A API exige alíquotas nominais a partir de 2027 e as proíbe antes disso.
        if dh.year >= 2027 and not aliq:
            raise ValueError("A partir de 2027 a calculadora exige [simulacao.aliquotas_nominais].")
        return cls(
            url_calculadora=calc["url"].rstrip("/"),
            timeout_s=float(calc.get("timeout_s", 30)),
            workers=int(calc.get("workers", 4)),
            dh_fato_gerador=dh,
            aliquotas=None if dh.year < 2027 else Aliquotas(
                cbs=Decimal(str(aliq["cbs"])),
                ibs_estadual=Decimal(str(aliq["ibs_estadual"])),
                ibs_municipal=Decimal(str(aliq["ibs_municipal"])),
            ),
            classificacao_csv=(caminho.parent / sim.get("classificacao_csv", "classificacao.csv")).resolve(),
            informar_tp_doc=bool(sim.get("informar_tp_doc", True)),
            tributos_remanescentes=_remanescentes(sim.get("tributos_remanescentes", [])),
            setor=sim.get("setor", "generico"),
            cadastro_produtos=_caminho(caminho, dados.get("saude", {}).get("cadastro_produtos")),
            cadastro_participantes=_caminho(caminho, dados.get("saude", {}).get("cadastro_participantes")),
            validar_ncm=bool(sim.get("validar_ncm", True)),
        )


def _caminho(config: Path, relativo: str | None) -> Path | None:
    return (config.parent / relativo).resolve() if relativo else None


TRIBUTOS_ATUAIS = {"ICMS", "ICMS_ST", "IPI", "PIS", "COFINS"}


def _remanescentes(nomes: list[str]) -> frozenset[str]:
    """Tributos atuais que continuam existindo no cenário simulado (ex.: ICMS até 2032)."""
    invalidos = set(nomes) - TRIBUTOS_ATUAIS
    if invalidos:
        raise ValueError(f"tributos_remanescentes inválidos: {sorted(invalidos)}; use {sorted(TRIBUTOS_ATUAIS)}")
    return frozenset(nomes)
