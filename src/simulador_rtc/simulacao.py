"""Orquestra a simulação: SPED -> classificação -> calculadora -> comparação."""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Iterator

import requests

from .classificacao import Classificacao
from .config import Config
from .rtc.client import CalculadoraClient
from .rtc.models import (AliquotasIS, AliquotasNominais, ComponentesBC, ImpostoSeletivo,
                         ItemOperacao, Operacao, ResultadoItem)
from .modelos import Documento, Empresa, Item

log = logging.getLogger(__name__)

SEM_CLASSIFICACAO = "SEM_CLASSIFICACAO"
ERRO_OPERACAO = "ERRO_OPERACAO"
NCM_INEXISTENTE = "NCM_INEXISTENTE"


@dataclass
class LinhaResultado:
    """Uma linha do relatório: o item do SPED, a classificação usada e as duas cargas."""
    documento: str
    data: str
    ind_oper: str
    participante: str
    item: Item
    classificacao: Classificacao | None
    estado: str
    v_bc_nova: Decimal = Decimal("0")
    v_cbs: Decimal = Decimal("0")
    v_ibs: Decimal = Decimal("0")
    v_is: Decimal = Decimal("0")
    remanescentes: Decimal = Decimal("0")  # tributos atuais que ainda existem no cenário
    erros: list[str] = field(default_factory=list)

    @property
    def alertas(self) -> tuple[str, ...]:
        return self.classificacao.alertas if self.classificacao else ()

    @property
    def carga_atual(self) -> Decimal:
        return self.item.carga_atual

    @property
    def carga_nova(self) -> Decimal:
        return self.v_cbs + self.v_ibs + self.v_is + self.remanescentes

    @property
    def diferenca(self) -> Decimal:
        return self.carga_nova - self.carga_atual


def _id_operacao(doc: Documento) -> str:
    return hashlib.md5(doc.identificador.encode()).hexdigest()


def _montar_item(numero: int, item: Item, cls: Classificacao, config: Config) -> ItemOperacao:
    is_cls = cls.imposto_seletivo
    aliq = None
    if config.aliquotas is not None:
        aliq = AliquotasNominais(
            cbs=config.aliquotas.cbs,
            ibsEstadual=config.aliquotas.ibs_estadual,
            ibsMunicipal=config.aliquotas.ibs_municipal,
            impostoSeletivo=AliquotasIS(adValorem=is_cls.ad_valorem) if is_cls and is_cls.ad_valorem else None,
        )
    comum = dict(numero=numero, ncm=item.ncm or None, cst=cls.cst, cClassTrib=cls.cclasstrib,
                 quantidade=item.quantidade or None, unidade=item.unidade or None, aliquotasNominais=aliq)

    if is_cls is not None:
        # Base do IS = mesma composição da base unificada oficial (valor - desconto - ICMS).
        # A base de CBS/IBS fica derivada pela calculadora: BC(IS) + IS.
        return ItemOperacao(**comum, impostoSeletivo=ImpostoSeletivo(
            cst=is_cls.cst, cClassTrib=is_cls.cclasstrib,
            baseCalculo=item.vl_item - item.vl_desc - item.vl_icms,
            quantidade=item.quantidade or None, unidade=item.unidade or None,
        ))
    return ItemOperacao(**comum, gComponentesBC=ComponentesBC(
        vProd=item.vl_item, vFrete=item.vl_frete or None, vSeg=item.vl_seg or None,
        vOutro=item.vl_outro or None, vDesc=item.vl_desc or None, vICMS=item.vl_icms or None,
        vFCP=item.vl_fcp or None, vPIS=item.vl_pis or None, vCOFINS=item.vl_cofins or None,
    ))


def montar_operacao(doc: Documento, empresa: Empresa, config: Config,
                    classificados: list[tuple[Item, Classificacao]]) -> Operacao:
    itens = [_montar_item(i, item, cls, config) for i, (item, cls) in enumerate(classificados, start=1)]
    tp_doc = int(doc.cod_mod) if config.informar_tp_doc and doc.cod_mod in ("55", "65") else None
    return Operacao(
        id=_id_operacao(doc),
        dhFatoGerador=config.dh_fato_gerador.isoformat(),
        municipio=int(doc.municipio_operacao or empresa.cod_mun),
        uf=doc.uf_operacao or empresa.uf,
        tpDoc=tp_doc,
        itens=itens,
    )


class Simulador:
    def __init__(self, config: Config, classificador, cliente: CalculadoraClient) -> None:
        """`classificador` é qualquer objeto com `classificar_item(item, doc)`."""
        self.config = config
        self.classificador = classificador
        self.cliente = cliente

    def _processar(self, doc: Documento, empresa: Empresa) -> list[LinhaResultado]:
        base = dict(documento=doc.identificador, data=doc.data, ind_oper=doc.ind_oper,
                    participante=doc.participante.cnpj if doc.participante else "")
        linhas: list[LinhaResultado] = []
        classificados: list[tuple[Item, Classificacao]] = []
        data = self.config.dh_fato_gerador.date().isoformat()
        for item in doc.itens:
            if self.config.validar_ncm and item.ncm and not self.cliente.ncm_existe(item.ncm, data):
                linhas.append(LinhaResultado(
                    item=item, classificacao=None, estado=NCM_INEXISTENTE,
                    erros=[f"NCM {item.ncm} não existe na tabela da calculadora para {data}: "
                           "corrija o cadastro do produto"], **base))
                continue
            cls = self.classificador.classificar_item(item, doc)
            if cls is None:
                linhas.append(LinhaResultado(item=item, classificacao=None, estado=SEM_CLASSIFICACAO,
                                             erros=["Nenhuma regra no de-para para este NCM/CFOP"], **base))
            else:
                classificados.append((item, cls))
        if not classificados:
            return linhas

        operacao = montar_operacao(doc, empresa, self.config, classificados)
        try:
            resultados = {r.n_obj: r for r in self.cliente.calcular(operacao)}
        except requests.HTTPError as exc:
            detalhe = exc.response.text[:500] if exc.response is not None else str(exc)
            for item, cls in classificados:
                linhas.append(LinhaResultado(item=item, classificacao=cls, estado=ERRO_OPERACAO,
                                             erros=[detalhe], **base))
            return linhas

        for n, (item, cls) in enumerate(classificados, start=1):
            r: ResultadoItem | None = resultados.get(n)
            if r is None:
                linhas.append(LinhaResultado(item=item, classificacao=cls, estado=ERRO_OPERACAO,
                                             erros=["Item ausente na resposta"], **base))
                continue
            linhas.append(LinhaResultado(
                item=item, classificacao=cls, estado=r.estado,
                v_bc_nova=r.v_bc, v_cbs=r.v_cbs, v_ibs=r.v_ibs, v_is=r.v_is,
                remanescentes=sum((item.valor_tributo(t) for t in self.config.tributos_remanescentes),
                                  Decimal("0")),
                erros=[f"{e.code}: {e.detail}" for e in r.erros], **base,
            ))
        return linhas

    def simular(self, documentos: Iterable[Documento], empresa_fn) -> Iterator[LinhaResultado]:
        """Processa documentos em paralelo (um POST por documento), mantendo a ordem.

        Usa uma janela limitada de envios para não carregar o arquivo inteiro em memória.
        """
        janela_max = self.config.workers * 4
        with ThreadPoolExecutor(max_workers=self.config.workers) as pool:
            janela: deque = deque()
            for doc in documentos:
                janela.append(pool.submit(self._processar, doc, empresa_fn()))
                if len(janela) >= janela_max:
                    yield from janela.popleft().result()
            while janela:
                yield from janela.popleft().result()
