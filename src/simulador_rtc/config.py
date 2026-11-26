"""Configuração da simulação (lida de um arquivo TOML).

Um arquivo pode descrever um cenário só ([simulacao]) ou vários ([[cenarios]]),
por exemplo um por ano da transição de 2026 a 2033. Cada cenário herda as chaves
de [simulacao] e sobrescreve as que declarar.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

UM = Decimal("1")
ZERO = Decimal("0")


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
    # Tributo atual -> fração que continua existindo no cenário (1 = integral; 0,9 = ICMS em 2029).
    tributos_remanescentes: dict[str, Decimal] = field(default_factory=dict)
    setor: str = "generico"
    cadastro_produtos: Path | None = None
    cadastro_participantes: Path | None = None
    validar_ncm: bool = True
    cenario: str = ""
    # Tributos atuais que a empresa credita hoje nas entradas (depende do regime: lucro real etc.).
    creditos_atuais: frozenset[str] = frozenset()
    # False no ano de teste (2026): a CBS/IBS destacada é compensável e não muda o custo.
    cbs_ibs_na_carga: bool = True
    versao_calculadora: str = ""  # versão esperada; vazio = não confere
    aprovacoes_csv: Path | None = None
    exigir_aprovacao: bool = False

    @property
    def data_fato_gerador(self) -> str:
        return self.dh_fato_gerador.date().isoformat()

    def fator(self, tributo: str) -> Decimal:
        return self.tributos_remanescentes.get(tributo, ZERO)

    @classmethod
    def de_toml(cls, caminho: str | Path) -> "Config":
        """Lê o cenário de [simulacao]. Para arquivos com [[cenarios]], use `carregar_cenarios`."""
        caminho = Path(caminho)
        dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
        return _montar(caminho, dados, dados["simulacao"])


def carregar_cenarios(caminho: str | Path) -> list[Config]:
    caminho = Path(caminho)
    dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
    base = dados.get("simulacao", {})
    cenarios = dados.get("cenarios")
    if not cenarios:
        return [_montar(caminho, dados, base)]
    nomes = [c.get("nome", "") for c in cenarios]
    if not all(nomes) or len(set(nomes)) != len(nomes):
        raise ValueError("Cada [[cenarios]] precisa de um `nome` único (vira o nome da pasta de saída).")
    return [_montar(caminho, dados, {**base, **c}) for c in cenarios]


def _montar(caminho: Path, dados: dict[str, Any], sim: dict[str, Any]) -> Config:
    calc = dados["calculadora"]
    governanca = dados.get("governanca", {})
    nome = sim.get("nome", "")
    if "dh_fato_gerador" not in sim:
        raise ValueError(f"Cenário {nome or '[simulacao]'}: falta dh_fato_gerador.")
    dh = datetime.fromisoformat(sim["dh_fato_gerador"])
    aliq = sim.get("aliquotas_nominais")
    # A API exige alíquotas nominais a partir de 2027 e as proíbe antes disso.
    if dh.year >= 2027 and not aliq:
        raise ValueError(f"Cenário {nome or '[simulacao]'}: a partir de 2027 a calculadora exige "
                         "aliquotas_nominais.")
    return Config(
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
        cenario=nome,
        creditos_atuais=_tributos(sim.get("creditos_atuais", []), "creditos_atuais"),
        cbs_ibs_na_carga=bool(sim.get("cbs_ibs_na_carga", True)),
        versao_calculadora=str(calc.get("versao_esperada", "")),
        aprovacoes_csv=_caminho(caminho, governanca.get("aprovacoes")),
        exigir_aprovacao=bool(governanca.get("exigir_aprovacao", False)),
    )


def _caminho(config: Path, relativo: str | None) -> Path | None:
    return (config.parent / relativo).resolve() if relativo else None


TRIBUTOS_ATUAIS = {"ICMS", "ICMS_ST", "IPI", "PIS", "COFINS"}


def _tributos(nomes: list[str], chave: str) -> frozenset[str]:
    invalidos = set(nomes) - TRIBUTOS_ATUAIS
    if invalidos:
        raise ValueError(f"{chave} inválidos: {sorted(invalidos)}; use {sorted(TRIBUTOS_ATUAIS)}")
    return frozenset(nomes)


def _remanescentes(valor: list[str] | dict[str, float]) -> dict[str, Decimal]:
    """Tributos atuais que continuam no cenário.

    Lista = integrais (ex.: ["ICMS", "ICMS_ST"] em 2027). Tabela = fração que resta
    (ex.: {ICMS = 0.9, ICMS_ST = 0.9} em 2029, na redução gradual do ICMS).
    """
    fatores = {n: UM for n in valor} if isinstance(valor, list) else \
        {n: Decimal(str(f)) for n, f in valor.items()}
    _tributos(list(fatores), "tributos_remanescentes")
    fora = {n: f for n, f in fatores.items() if not ZERO <= f <= UM}
    if fora:
        raise ValueError(f"tributos_remanescentes: frações devem ficar entre 0 e 1: {fora}")
    return fatores
