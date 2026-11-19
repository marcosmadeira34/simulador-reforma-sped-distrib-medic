"""Cliente HTTP da Calculadora RTC rodando localmente (perfil offline)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from .models import Operacao, ResultadoItem

log = logging.getLogger(__name__)


class CalculadoraIndisponivel(RuntimeError):
    pass


class CalculadoraClient:
    def __init__(self, url_base: str = "http://localhost:8080/api", timeout_s: float = 30,
                 tentativas: int = 3, sessao: requests.Session | None = None) -> None:
        self.url = url_base.rstrip("/") + "/calculadora"
        self.timeout = timeout_s
        self.tentativas = tentativas
        self.sessao = sessao or requests.Session()
        self._cache_ncm: dict[tuple[str, str, str], bool] = {}
        self._cache_existe: dict[tuple[str, str], bool] = {}
        self._lock = threading.Lock()

    def _post(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        ultimo_erro: Exception | None = None
        for tentativa in range(1, self.tentativas + 1):
            try:
                resp = self.sessao.post(f"{self.url}{caminho}", json=corpo, timeout=self.timeout)
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                return resp.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                ultimo_erro = exc
                if isinstance(exc, requests.HTTPError) and exc.response is not None \
                        and exc.response.status_code < 500:
                    raise  # erro de entrada (4xx): não adianta repetir
                espera = 0.5 * 2 ** (tentativa - 1)
                log.warning("Calculadora falhou (tentativa %s/%s): %s", tentativa, self.tentativas, exc)
                time.sleep(espera)
        raise CalculadoraIndisponivel(str(ultimo_erro))

    def calcular(self, operacao: Operacao) -> list[ResultadoItem]:
        """Calcula pelo endpoint de observabilidade: erros vêm por item, sem derrubar a operação."""
        resposta = self._post("/observabilidade/regime-geral", operacao.para_api())
        return [ResultadoItem.da_api(o) for o in resposta.get("objetos", [])]

    def ncm_aplicavel(self, cclasstrib: str, ncm: str, data: str) -> bool:
        """Pergunta à calculadora se o NCM é aceito no cClassTrib (com cache por execução).

        Atenção: para medicamentos a calculadora aceita qualquer NCM; o resultado só
        discrimina classificações que são listas de NCM (dispositivos, acessibilidade...).
        """
        chave = (cclasstrib, ncm, data)
        with self._lock:
            if chave in self._cache_ncm:
                return self._cache_ncm[chave]
        r = self.sessao.get(f"{self.url}/dados-abertos/classificacoes-tributarias/ncm-aplicavel",
                            params={"cClassTrib": cclasstrib, "ncm": ncm, "dataOcorrenciaFatoGerador": data},
                            timeout=self.timeout)
        r.raise_for_status()
        valido = bool(r.json().get("valido"))
        with self._lock:
            self._cache_ncm[chave] = valido
        return valido

    def ncm_existe(self, ncm: str, data: str) -> bool:
        """Confere se o NCM existe na tabela da calculadora na data (com cache por execução).

        Necessário porque o ncm-aplicavel só confere a lista do anexo e responde
        valido=true até para NCM extinto; o cálculo depois falha com REG-002.
        """
        chave = (ncm, data)
        with self._lock:
            if chave in self._cache_existe:
                return self._cache_existe[chave]
        r = self.sessao.get(f"{self.url}/dados-abertos/ncm", params={"ncm": ncm, "data": data},
                            timeout=self.timeout)
        if r.status_code == 200:
            existe = True
        elif r.status_code == 404:
            existe = False
        else:
            r.raise_for_status()
            raise requests.HTTPError(f"HTTP {r.status_code} inesperado em /dados-abertos/ncm", response=r)
        with self._lock:
            self._cache_existe[chave] = existe
        return existe

    def disponivel(self) -> bool:
        try:
            r = self.sessao.get(f"{self.url}/dados-abertos/situacoes-tributarias/cbs-ibs", timeout=5)
            return r.status_code < 500
        except requests.RequestException:
            return False
