"""Modelos comuns a todas as fontes de documentos (EFD ICMS/IPI e XML de NF-e)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")


@dataclass
class Empresa:
    cnpj: str
    nome: str
    uf: str
    cod_mun: str
    dt_ini: str = ""
    dt_fin: str = ""


@dataclass
class Participante:
    cnpj: str
    nome: str
    uf: str = ""
    cod_mun: str = ""


@dataclass
class Item:
    num_item: str
    cod_item: str
    descricao: str
    ncm: str
    cfop: str
    quantidade: Decimal
    unidade: str
    vl_item: Decimal
    vl_desc: Decimal
    vl_icms: Decimal
    vl_icms_st: Decimal
    vl_ipi: Decimal
    vl_pis: Decimal
    vl_cofins: Decimal
    vl_frete: Decimal = ZERO
    vl_seg: Decimal = ZERO
    vl_outro: Decimal = ZERO
    vl_fcp: Decimal = ZERO
    registro_anvisa: str = ""   # NF-e: det/prod/med/cProdANVISA
    pmc: Decimal = ZERO         # NF-e: det/prod/med/vPMC

    def valor_tributo(self, nome: str) -> Decimal:
        return {"ICMS": self.vl_icms, "ICMS_ST": self.vl_icms_st, "IPI": self.vl_ipi,
                "PIS": self.vl_pis, "COFINS": self.vl_cofins}[nome]

    @property
    def carga_atual(self) -> Decimal:
        """Tributos atuais destacados no item: ICMS + ICMS-ST + IPI + PIS + COFINS."""
        return self.vl_icms + self.vl_icms_st + self.vl_ipi + self.vl_pis + self.vl_cofins


@dataclass
class Documento:
    ind_oper: str  # 0 = entrada, 1 = saída
    cod_mod: str
    cod_sit: str
    serie: str
    numero: str
    chave: str
    data: str
    itens: list[Item] = field(default_factory=list)
    # Contraparte da operação: destinatário na saída, fornecedor na entrada.
    participante: Participante | None = None
    # Local da operação para o IBS. Sem valor, usa o município da empresa.
    uf_operacao: str = ""
    municipio_operacao: str = ""

    @property
    def identificador(self) -> str:
        return self.chave or f"{self.cod_mod}-{self.serie}-{self.numero}-{self.data}"
