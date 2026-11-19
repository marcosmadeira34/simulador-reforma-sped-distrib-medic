"""Modelos da API da Calculadora RTC (regime regular).

Só os campos usados pelo simulador. O contrato completo está no Swagger da
calculadora: http://localhost:8080/api/swagger-ui.html
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ESTADOS_CALCULADOS = {"CALCULADO", "CALCULO_SIMULADO"}


def _numeros(valor: Any) -> Any:
    if isinstance(valor, Decimal):
        return int(valor) if valor == valor.to_integral_value() and valor.as_tuple().exponent >= 0 \
            else float(valor)
    if isinstance(valor, dict):
        return {k: _numeros(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_numeros(v) for v in valor]
    return valor


class _Modelo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    def para_api(self) -> dict[str, Any]:
        """Dict pronto para JSON, com valores como números (a API espera BigDecimal numérico).

        Python serializa float pela menor representação exata ("23.93"), então valores
        com 2 casas chegam sem ruído de ponto flutuante.
        """
        return _numeros(self.model_dump(by_alias=True, exclude_none=True))


class AliquotasIS(_Modelo):
    ad_valorem: Decimal | None = Field(default=None, alias="adValorem")
    ad_rem: Decimal | None = Field(default=None, alias="adRem")


class AliquotasNominais(_Modelo):
    cbs: Decimal
    ibs_estadual: Decimal = Field(alias="ibsEstadual")
    ibs_municipal: Decimal = Field(alias="ibsMunicipal")
    imposto_seletivo: AliquotasIS | None = Field(default=None, alias="impostoSeletivo")


class ImpostoSeletivo(_Modelo):
    """Grupo do IS. Com ele (e sem gComponentesBC), a base de CBS/IBS é derivada: BC(IS) + IS."""
    cst: str
    c_class_trib: str = Field(alias="cClassTrib")
    base_calculo: Decimal = Field(alias="baseCalculo")
    quantidade: Decimal | None = None
    unidade: str | None = None


class ComponentesBC(_Modelo):
    """Componentes da base; a calculadora aplica a vigência de cada um ao ano do fato gerador."""
    v_prod: Decimal = Field(alias="vProd")
    v_frete: Decimal | None = Field(default=None, alias="vFrete")
    v_seg: Decimal | None = Field(default=None, alias="vSeg")
    v_outro: Decimal | None = Field(default=None, alias="vOutro")
    v_desc: Decimal | None = Field(default=None, alias="vDesc")
    v_icms: Decimal | None = Field(default=None, alias="vICMS")
    v_fcp: Decimal | None = Field(default=None, alias="vFCP")
    v_pis: Decimal | None = Field(default=None, alias="vPIS")
    v_cofins: Decimal | None = Field(default=None, alias="vCOFINS")


class ItemOperacao(_Modelo):
    numero: int
    ncm: str | None = None
    cst: str
    c_class_trib: str = Field(alias="cClassTrib")
    quantidade: Decimal | None = None
    unidade: str | None = None
    componentes_bc: ComponentesBC | None = Field(default=None, alias="gComponentesBC")
    imposto_seletivo: ImpostoSeletivo | None = Field(default=None, alias="impostoSeletivo")
    aliquotas_nominais: AliquotasNominais | None = Field(default=None, alias="aliquotasNominais")


class Operacao(_Modelo):
    id: str
    versao: str = "1.0.0"
    dh_fato_gerador: str = Field(alias="dhFatoGerador")
    municipio: int
    uf: str
    tp_doc: int | None = Field(default=None, alias="tpDoc")
    itens: list[ItemOperacao]


class ErroItem(BaseModel):
    code: str | None = None
    title: str | None = None
    detail: str | None = None


class ResultadoItem(BaseModel):
    """Resultado de um item no endpoint de observabilidade."""
    n_obj: int
    estado: str
    v_bc: Decimal = Decimal("0")
    v_cbs: Decimal = Decimal("0")
    v_ibs: Decimal = Decimal("0")
    v_is: Decimal = Decimal("0")
    erros: list[ErroItem] = []

    @property
    def calculado(self) -> bool:
        return self.estado in ESTADOS_CALCULADOS

    @property
    def carga_nova(self) -> Decimal:
        return self.v_cbs + self.v_ibs + self.v_is

    @classmethod
    def da_api(cls, obj: dict[str, Any]) -> "ResultadoItem":
        trib = obj.get("tribCalc") or {}
        g = (trib.get("IBSCBS") or {}).get("gIBSCBS") or {}
        d = lambda v: Decimal(str(v)) if v not in (None, "") else Decimal("0")  # noqa: E731
        return cls(
            n_obj=obj["nObj"],
            estado=obj.get("estadoItem", "CALCULADO"),
            v_bc=d(g.get("vBC")),
            v_cbs=d((g.get("gCBS") or {}).get("vCBS")),
            v_ibs=d(g.get("vIBS")),
            v_is=d((trib.get("IS") or {}).get("vIS")),
            erros=[ErroItem(**e) for e in obj.get("erros") or []],
        )
