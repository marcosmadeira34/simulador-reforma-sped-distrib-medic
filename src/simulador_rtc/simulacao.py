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
from .governanca import Governanca
from .rtc.client import CalculadoraClient
from .rtc.models import (AliquotasIS, AliquotasNominais, ComponentesBC, ImpostoSeletivo,
                         ItemOperacao, Operacao, ResultadoItem)
from .modelos import Documento, Empresa, Item

log = logging.getLogger(__name__)

SEM_CLASSIFICACAO = "SEM_CLASSIFICACAO"
ERRO_OPERACAO = "ERRO_OPERACAO"
NCM_INEXISTENTE = "NCM_INEXISTENTE"

DEBITO = "DEBITO"    # saída: tributo que a empresa paga
CREDITO = "CREDITO"  # entrada: tributo que a empresa recupera
ZERO = Decimal("0")


@dataclass
class LinhaResultado:
    """Uma linha do relatório: o item do SPED, a classificação usada e as duas cargas.

    `carga_atual`/`carga_nova` são os tributos do item (débito na saída, valor destacado
    na compra). `credito_atual`/`credito_novo` são a parte que a empresa recupera nas
    entradas, hoje e no cenário.
    """
    documento: str
    data: str
    ind_oper: str
    participante: str
    item: Item
    classificacao: Classificacao | None
    estado: str
    v_bc_nova: Decimal = ZERO
    v_cbs: Decimal = ZERO
    v_ibs: Decimal = ZERO
    v_is: Decimal = ZERO
    remanescentes: Decimal = ZERO  # tributos atuais que ainda existem no cenário
    credito_atual: Decimal = ZERO
    credito_novo: Decimal = ZERO
    cbs_ibs_na_carga: bool = True  # False no ano de teste (CBS/IBS compensáveis)
    erros: list[str] = field(default_factory=list)

    @property
    def natureza(self) -> str:
        return CREDITO if self.ind_oper == "0" else DEBITO

    @property
    def alertas(self) -> tuple[str, ...]:
        return self.classificacao.alertas if self.classificacao else ()

    @property
    def carga_atual(self) -> Decimal:
        return self.item.carga_atual

    @property
    def carga_nova(self) -> Decimal:
        cbs_ibs = self.v_cbs + self.v_ibs if self.cbs_ibs_na_carga else ZERO
        return cbs_ibs + self.v_is + self.remanescentes

    @property
    def diferenca(self) -> Decimal:
        return self.carga_nova - self.carga_atual


def _id_operacao(doc: Documento) -> str:
    return hashlib.md5(doc.identificador.encode()).hexdigest()


def _fator_icms(config: Config) -> Decimal:
    """Fração do ICMS de hoje que existe no cenário, para a base de CBS/IBS.

    A calculadora aplica sozinha a vigência dos componentes (em 2033 ela já ignora o
    ICMS), mas não conhece a redução gradual de 2029 a 2032. Sem o ICMS nos
    remanescentes, o valor vai inteiro e a calculadora decide pela data.
    """
    return config.tributos_remanescentes.get("ICMS", Decimal("1"))


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
    fator = _fator_icms(config)
    v_icms, v_fcp = item.vl_icms * fator, item.vl_fcp * fator

    if is_cls is not None:
        # Base do IS = mesma composição da base unificada oficial (valor - desconto - ICMS).
        # A base de CBS/IBS fica derivada pela calculadora: BC(IS) + IS.
        return ItemOperacao(**comum, impostoSeletivo=ImpostoSeletivo(
            cst=is_cls.cst, cClassTrib=is_cls.cclasstrib,
            baseCalculo=item.vl_item - item.vl_desc - v_icms,
            quantidade=item.quantidade or None, unidade=item.unidade or None,
        ))
    return ItemOperacao(**comum, gComponentesBC=ComponentesBC(
        vProd=item.vl_item, vFrete=item.vl_frete or None, vSeg=item.vl_seg or None,
        vOutro=item.vl_outro or None, vDesc=item.vl_desc or None, vICMS=v_icms or None,
        vFCP=v_fcp or None, vPIS=item.vl_pis or None, vCOFINS=item.vl_cofins or None,
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
    def __init__(self, config: Config, classificador, cliente: CalculadoraClient,
                 governanca: Governanca | None = None) -> None:
        """`classificador` é qualquer objeto com `classificar_item(item, doc)`."""
        self.config = config
        self.classificador = classificador
        self.cliente = cliente
        self.governanca = governanca or Governanca(cliente, config.data_fato_gerador)

    def _remanescentes(self, item: Item) -> Decimal:
        return sum((item.valor_tributo(t) * f for t, f in self.config.tributos_remanescentes.items()), ZERO)

    def _creditos(self, item: Item, cls: Classificacao, r: ResultadoItem) -> tuple[Decimal, Decimal]:
        """(crédito hoje, crédito no cenário) de um item de entrada."""
        cfg = self.config
        atual = sum((item.valor_tributo(t) for t in cfg.creditos_atuais), ZERO)
        novo = sum((item.valor_tributo(t) * cfg.fator(t) for t in cfg.creditos_atuais), ZERO)
        if cfg.cbs_ibs_na_carga:
            cbs_ok, ibs_ok = self.cliente.credito_adquirente(cls.cclasstrib, cfg.data_fato_gerador)
            novo += (r.v_cbs if cbs_ok else ZERO) + (r.v_ibs if ibs_ok else ZERO)
        return atual, novo  # IS não gera crédito

    def _processar(self, doc: Documento, empresa: Empresa) -> list[LinhaResultado]:
        base = dict(documento=doc.identificador, data=doc.data, ind_oper=doc.ind_oper,
                    participante=doc.participante.cnpj if doc.participante else "")
        linhas: list[LinhaResultado] = []
        classificados: list[tuple[Item, Classificacao]] = []
        data = self.config.data_fato_gerador
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
                classificados.append((item, self.governanca.aplicar(cls)))
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
            credito_atual, credito_novo = ZERO, ZERO
            if doc.ind_oper == "0" and r.calculado:
                credito_atual, credito_novo = self._creditos(item, cls, r)
            linhas.append(LinhaResultado(
                item=item, classificacao=cls, estado=r.estado,
                v_bc_nova=r.v_bc, v_cbs=r.v_cbs, v_ibs=r.v_ibs, v_is=r.v_is,
                remanescentes=self._remanescentes(item),
                credito_atual=credito_atual, credito_novo=credito_novo,
                cbs_ibs_na_carga=self.config.cbs_ibs_na_carga,
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
