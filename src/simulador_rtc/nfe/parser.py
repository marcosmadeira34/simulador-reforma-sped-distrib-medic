"""Leitura de XML de NF-e (modelo 55/65), autorizadas (nfeProc) ou não.

É a fonte das **saídas próprias**: na EFD ICMS/IPI essas notas vêm só com C190,
sem itens. O XML traz por item o NCM, os valores, os tributos e, para
medicamentos, o grupo `med` com o registro na Anvisa (`cProdANVISA`) e o PMC.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from ..modelos import ZERO, Documento, Empresa, Item, Participante

NS = {"n": "http://www.portalfiscal.inf.br/nfe"}


def _t(no: ET.Element | None, caminho: str) -> str:
    if no is None:
        return ""
    achado = no.find(caminho, NS)
    return (achado.text or "").strip() if achado is not None else ""


def _d(no: ET.Element | None, caminho: str) -> Decimal:
    texto = _t(no, caminho)
    return Decimal(texto) if texto else ZERO


class NfeParser:
    """Lê um arquivo XML ou todos os `.xml` de uma pasta."""

    def __init__(self, caminho: str | Path) -> None:
        self.caminho = Path(caminho)
        self.empresa: Empresa | None = None
        self.ignorados: list[tuple[str, str]] = []  # (arquivo, motivo)

    def arquivos(self) -> list[Path]:
        if self.caminho.is_dir():
            return sorted(self.caminho.glob("*.xml"))
        return [self.caminho]

    def documentos(self) -> Iterator[Documento]:
        for arquivo in self.arquivos():
            try:
                raiz = ET.parse(arquivo).getroot()
            except ET.ParseError as exc:
                self.ignorados.append((arquivo.name, f"XML inválido: {exc}"))
                continue
            inf = raiz.find(".//n:infNFe", NS)
            if inf is None:
                self.ignorados.append((arquivo.name, "não é NF-e (infNFe ausente)"))
                continue
            yield self._documento(inf)

    def _documento(self, inf: ET.Element) -> Documento:
        ide, emit, dest = inf.find("n:ide", NS), inf.find("n:emit", NS), inf.find("n:dest", NS)
        if self.empresa is None:
            self.empresa = Empresa(
                cnpj=_t(emit, "n:CNPJ"), nome=_t(emit, "n:xNome"),
                uf=_t(emit, "n:enderEmit/n:UF"), cod_mun=_t(emit, "n:enderEmit/n:cMun"),
            )
        participante = None
        if dest is not None:
            participante = Participante(
                cnpj=_t(dest, "n:CNPJ") or _t(dest, "n:CPF"), nome=_t(dest, "n:xNome"),
                uf=_t(dest, "n:enderDest/n:UF"), cod_mun=_t(dest, "n:enderDest/n:cMun"),
            )
        doc = Documento(
            ind_oper="0" if _t(ide, "n:tpNF") == "0" else "1",
            cod_mod=_t(ide, "n:mod"), cod_sit="00", serie=_t(ide, "n:serie"),
            numero=_t(ide, "n:nNF"), chave=inf.get("Id", "").removeprefix("NFe"),
            data=_t(ide, "n:dhEmi")[:10],
            participante=participante,
            # Bens: o IBS vai para o destino. Sem destinatário (NFC-e), fica o local do emitente.
            uf_operacao=participante.uf if participante and participante.uf else "",
            municipio_operacao=participante.cod_mun if participante and participante.cod_mun else "",
        )
        for det in inf.findall("n:det", NS):
            doc.itens.append(self._item(det))
        return doc

    def _item(self, det: ET.Element) -> Item:
        prod, imp = det.find("n:prod", NS), det.find("n:imposto", NS)
        icms = imp.find("n:ICMS/*", NS) if imp is not None else None  # ICMS00, ICMS10, ICMS60...
        return Item(
            num_item=det.get("nItem", ""),
            cod_item=_t(prod, "n:cProd"),
            descricao=_t(prod, "n:xProd"),
            ncm=_t(prod, "n:NCM"),
            cfop=_t(prod, "n:CFOP"),
            quantidade=_d(prod, "n:qCom"),
            unidade=_t(prod, "n:uCom"),
            vl_item=_d(prod, "n:vProd"),
            vl_desc=_d(prod, "n:vDesc"),
            vl_frete=_d(prod, "n:vFrete"),
            vl_seg=_d(prod, "n:vSeg"),
            vl_outro=_d(prod, "n:vOutro"),
            vl_icms=_d(icms, "n:vICMS"),
            vl_icms_st=_d(icms, "n:vICMSST"),
            vl_fcp=_d(icms, "n:vFCP"),
            vl_ipi=_d(imp, "n:IPI/n:IPITrib/n:vIPI"),
            vl_pis=_d(imp, "n:PIS/*/n:vPIS"),
            vl_cofins=_d(imp, "n:COFINS/*/n:vCOFINS"),
            registro_anvisa=_t(prod, "n:med/n:cProdANVISA"),
            pmc=_d(prod, "n:med/n:vPMC"),
        )
