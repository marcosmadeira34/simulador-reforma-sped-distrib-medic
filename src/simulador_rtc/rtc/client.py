"""Cliente HTTP da Calculadora RTC rodando localmente (perfil offline)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, TypeVar

import requests

from .models import Operacao, ResultadoItem

log = logging.getLogger(__name__)

T = TypeVar("T")


class CalculadoraIndisponivel(RuntimeError):
    pass


class CalculadoraClient:
    def __init__(self, url_base: str = "http://localhost:8080/api", timeout_s: float = 30,
                 tentativas: int = 3, sessao: requests.Session | None = None,
                 dormir: Callable[[float], None] = time.sleep) -> None:
        self.url = url_base.rstrip("/") + "/calculadora"
        self.timeout = timeout_s
        self.tentativas = tentativas
        self.sessao = sessao or requests.Session()
        self._dormir = dormir  # injetável para os testes não esperarem de verdade
        self._cache_ncm: dict[tuple[str, str, str], bool] = {}
        self._cache_existe: dict[tuple[str, str], bool] = {}
        self._cache_classificacao: dict[tuple[str, str], dict[str, Any]] = {}
        self._cache_fundamentacao: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    # -- transporte ---------------------------------------------------------
    def _requisitar(self, metodo: str, caminho: str, *, aceitar: tuple[int, ...] = (),
                    **kwargs: Any) -> requests.Response:
        """Faz a chamada repetindo falhas transitórias (conexão, timeout e HTTP 5xx).

        Erros 4xx são de entrada: não adianta repetir. Os códigos em `aceitar`
        (ex.: 404 na consulta de NCM) voltam como resposta normal.
        """
        ultimo_erro: Exception | None = None
        for tentativa in range(1, self.tentativas + 1):
            try:
                resp = self.sessao.request(metodo, f"{self.url}{caminho}", timeout=self.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                ultimo_erro = exc
            else:
                if 200 <= resp.status_code < 300 or resp.status_code in aceitar:
                    return resp
                if resp.status_code < 500:
                    resp.raise_for_status()
                    raise requests.HTTPError(f"HTTP {resp.status_code} inesperado em {caminho}", response=resp)
                ultimo_erro = requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:300]}", response=resp)
            if tentativa < self.tentativas:
                espera = 0.5 * 2 ** (tentativa - 1)
                log.warning("Calculadora falhou em %s (tentativa %s/%s): %s",
                            caminho, tentativa, self.tentativas, ultimo_erro)
                self._dormir(espera)
        raise CalculadoraIndisponivel(f"{metodo} {caminho}: {ultimo_erro}")

    def _get(self, caminho: str, params: dict[str, str] | None = None, **kwargs: Any) -> requests.Response:
        return self._requisitar("GET", caminho, params=params, **kwargs)

    def _memo(self, cache: dict, chave: Any, buscar: Callable[[], T]) -> T:
        with self._lock:
            if chave in cache:
                return cache[chave]
        valor = buscar()  # fora do lock: consultas diferentes seguem em paralelo
        with self._lock:
            cache[chave] = valor
        return valor

    # -- cálculo ------------------------------------------------------------
    def calcular(self, operacao: Operacao) -> list[ResultadoItem]:
        """Calcula pelo endpoint de observabilidade: erros vêm por item, sem derrubar a operação."""
        resposta = self._requisitar("POST", "/observabilidade/regime-geral", json=operacao.para_api()).json()
        return [ResultadoItem.da_api(o) for o in resposta.get("objetos", [])]

    # -- dados abertos ------------------------------------------------------
    def ncm_aplicavel(self, cclasstrib: str, ncm: str, data: str) -> bool:
        """Pergunta à calculadora se o NCM é aceito no cClassTrib (com cache por execução).

        Atenção: para medicamentos a calculadora aceita qualquer NCM; o resultado só
        discrimina classificações que são listas de NCM (dispositivos, acessibilidade...).
        """
        def buscar() -> bool:
            r = self._get("/dados-abertos/classificacoes-tributarias/ncm-aplicavel",
                          {"cClassTrib": cclasstrib, "ncm": ncm, "dataOcorrenciaFatoGerador": data})
            return bool(r.json().get("valido"))
        return self._memo(self._cache_ncm, (cclasstrib, ncm, data), buscar)

    def ncm_existe(self, ncm: str, data: str) -> bool:
        """Confere se o NCM existe na tabela da calculadora na data (com cache por execução).

        Necessário porque o ncm-aplicavel só confere a lista do anexo e responde
        valido=true até para NCM extinto; o cálculo depois falha com REG-002.
        """
        def buscar() -> bool:
            return self._get("/dados-abertos/ncm", {"ncm": ncm, "data": data}, aceitar=(404,)).status_code != 404
        return self._memo(self._cache_existe, (ncm, data), buscar)

    def classificacao(self, cclasstrib: str, data: str) -> dict[str, Any]:
        """Ficha do cClassTrib na data: redução, indicadores de crédito etc. (com cache)."""
        def buscar() -> dict[str, Any]:
            return self._get(f"/dados-abertos/classificacoes-tributarias/cbs-ibs/class-trib/{cclasstrib}",
                             {"data": data}).json()
        return self._memo(self._cache_classificacao, (cclasstrib, data), buscar)

    def credito_adquirente(self, cclasstrib: str, data: str) -> tuple[bool, bool]:
        """(CBS, IBS): se quem compra com esse cClassTrib pode apropriar o crédito."""
        ficha = self.classificacao(cclasstrib, data)
        return (bool(ficha.get("indicaApropriacaoCreditoAdquirenteCbs")),
                bool(ficha.get("indicaApropriacaoCreditoAdquirenteIbs")))

    def fundamentacao(self, cclasstrib: str, data: str) -> str:
        """Base legal do cClassTrib segundo a própria calculadora (ex.: 'LC 214/2025, Art. 133')."""
        def buscar() -> dict[str, str]:
            por_codigo: dict[str, list[str]] = {}
            for f in self._get("/dados-abertos/fundamentacoes-legais", {"data": data}).json():
                curto = (f.get("textoCurto") or "").strip()
                if curto:
                    lista = por_codigo.setdefault(f["codigoClassificacaoTributaria"], [])
                    if curto not in lista:
                        lista.append(curto)
            return {cc: "; ".join(t if t.startswith("LC ") else f"LC 214/2025, {t}" for t in textos)
                    for cc, textos in por_codigo.items()}
        return self._memo(self._cache_fundamentacao, data, buscar).get(cclasstrib, "")

    def versao(self) -> dict[str, Any]:
        """Versão da aplicação e da base de dados da calculadora (ex.: 1.6.1 / V0059)."""
        return self._get("/dados-abertos/versao").json()

    def disponivel(self) -> bool:
        try:
            r = self.sessao.get(f"{self.url}/dados-abertos/situacoes-tributarias/cbs-ibs", timeout=5)
            return r.status_code < 500
        except requests.RequestException:
            return False
