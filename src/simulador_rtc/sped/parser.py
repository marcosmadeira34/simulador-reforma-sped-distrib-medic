"""Leitura em streaming da EFD ICMS/IPI.

O parser percorre o arquivo linha a linha (sem carregar tudo em memória) e
entrega um `Documento` por registro C100, já com os itens C170 e o NCM de cada
produto (registro 0200).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from ..modelos import Documento, Empresa, Item, Participante
from .layout import COD_SIT_VALIDOS, LEIAUTES

__all__ = ["EfdIcmsIpiParser", "Documento", "Empresa", "Item"]


def _decimal(valor: str) -> Decimal:
    """Converte '1.234,56' ou '1234,56' em Decimal. Vazio vira zero."""
    if not valor:
        return Decimal("0")
    try:
        return Decimal(valor.replace(".", "").replace(",", "."))
    except InvalidOperation as exc:  # pragma: no cover - dado corrompido
        raise ValueError(f"Valor numérico inválido no SPED: {valor!r}") from exc


def _registro(linha: str) -> dict[str, str] | None:
    """Transforma '|C170|1|...|' em dict pelos nomes do leiaute. Ignora registros não usados."""
    linha = linha.strip()
    if not linha.startswith("|"):
        return None
    campos = linha.split("|")[1:-1]
    if not campos:
        return None
    leiaute = LEIAUTES.get(campos[0])
    if leiaute is None:
        return None
    campos += [""] * (len(leiaute) - len(campos))  # leiautes antigos têm menos campos
    return dict(zip(leiaute, campos))


@dataclass
class ResultadoLeitura:
    empresa: Empresa | None
    documentos_ignorados: int = 0
    # C100 válidos sem nenhum C170 (em geral NF-e própria, que só traz C190).
    # Guardamos o identificador para conferir se o XML da nota foi lido.
    documentos_sem_itens: list[str] = field(default_factory=list)


class EfdIcmsIpiParser:
    """Lê uma EFD ICMS/IPI e entrega documentos com itens.

    Atenção: na EFD ICMS/IPI o C170 normalmente existe nas **entradas** e nas
    saídas que não são NF-e própria. NF-e de emissão própria vem só com C190;
    para essas, o caminho é ler o XML da nota (veja o roadmap no README).
    """

    def __init__(self, caminho: str | Path, encoding: str = "latin-1") -> None:
        self.caminho = Path(caminho)
        self.encoding = encoding
        self.produtos: dict[str, dict[str, str]] = {}
        self.participantes: dict[str, Participante] = {}
        self.info = ResultadoLeitura(empresa=None)

    def documentos(self) -> Iterator[Documento]:
        atual: Documento | None = None
        with self.caminho.open(encoding=self.encoding) as arquivo:
            for linha in arquivo:
                reg = _registro(linha)
                if reg is None:
                    continue
                tipo = reg["REG"]
                if tipo == "0000":
                    self.info.empresa = Empresa(
                        cnpj=reg["CNPJ"], nome=reg["NOME"], uf=reg["UF"],
                        cod_mun=reg["COD_MUN"], dt_ini=reg["DT_INI"], dt_fin=reg["DT_FIN"],
                    )
                elif tipo == "0150":
                    self.participantes[reg["COD_PART"]] = Participante(
                        cnpj=reg["CNPJ"] or reg["CPF"], nome=reg["NOME"], cod_mun=reg["COD_MUN"])
                elif tipo == "0200":
                    self.produtos[reg["COD_ITEM"]] = reg
                elif tipo == "C100":
                    if atual is not None:
                        yield from self._fechar(atual)
                    if reg["COD_SIT"] not in COD_SIT_VALIDOS:
                        self.info.documentos_ignorados += 1
                        atual = None
                        continue
                    atual = Documento(
                        ind_oper=reg["IND_OPER"], cod_mod=reg["COD_MOD"], cod_sit=reg["COD_SIT"],
                        serie=reg["SER"], numero=reg["NUM_DOC"], chave=reg["CHV_NFE"], data=reg["DT_DOC"],
                        participante=self.participantes.get(reg["COD_PART"]),
                    )
                elif tipo == "C170" and atual is not None:
                    atual.itens.append(self._item(reg))
        if atual is not None:
            yield from self._fechar(atual)

    def _fechar(self, doc: Documento) -> Iterator[Documento]:
        if doc.itens:
            yield doc
        else:
            self.info.documentos_sem_itens.append(doc.identificador)

    def _item(self, reg: dict[str, str]) -> Item:
        produto = self.produtos.get(reg["COD_ITEM"], {})
        return Item(
            num_item=reg["NUM_ITEM"],
            cod_item=reg["COD_ITEM"],
            descricao=produto.get("DESCR_ITEM", reg["DESCR_COMPL"]),
            ncm=produto.get("COD_NCM", ""),
            cfop=reg["CFOP"],
            quantidade=_decimal(reg["QTD"]),
            unidade=reg["UNID"],
            vl_item=_decimal(reg["VL_ITEM"]),
            vl_desc=_decimal(reg["VL_DESC"]),
            vl_icms=_decimal(reg["VL_ICMS"]),
            vl_icms_st=_decimal(reg["VL_ICMS_ST"]),
            vl_ipi=_decimal(reg["VL_IPI"]),
            vl_pis=_decimal(reg["VL_PIS"]),
            vl_cofins=_decimal(reg["VL_COFINS"]),
        )
