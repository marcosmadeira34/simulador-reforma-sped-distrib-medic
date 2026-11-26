"""Dublê de testes da Calculadora RTC.

NÃO calcula tributos de verdade: aplica uma regra simplificada só para testar o
fluxo do simulador (base = vProd - vDesc - vICMS; reduções por cClassTrib).
Os comportamentos que o simulador depende (2026 sem alíquotas nominais, ICMS fora
da base em 2033, NCM extinto, indicadores de crédito, versão) imitam a calculadora
1.6.1 e são conferidos contra a real em `test_contrato.py`.
Para resultados reais, use a calculadora oficial.
"""

from __future__ import annotations

import json
import threading
from decimal import ROUND_HALF_UP, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

D2 = Decimal("0.01")

# Reduções por cClassTrib (fator aplicado sobre a alíquota). Espelha a tabela oficial.
ZERO = {"200003", "200004", "200005", "200007", "200008", "200009", "200010", "200011", "200053"}
SESSENTA = {"200030", "200031", "200032", "200033"}
MEDICAMENTOS = {"200009", "200010", "200012", "200032", "200053"}
# Classificações em que o adquirente NÃO apropria crédito (observado na 1.6.1: 200053 e 410999).
SEM_CREDITO = {"200053", "410999"}

NCM_INEXISTENTES = {"30022029", "40151100"}  # fora da tabela de NCM em 2027

# ncm-aplicavel: pares observados na calculadora 1.6.1 (seringa e cadeira de rodas)
# e pares assumidos só para o teste (dieta enteral).
NCM_ACEITO = {
    ("200030", "90183119"), ("200030", "30049099"), ("200007", "87139000"),
    ("200033", "21069090"), ("200011", "21069090"),
}

# Fundamentação legal (textoCurto) observada na calculadora 1.6.1.
FUNDAMENTACAO = {
    "000001": "LC 214/2025", "200003": "Art. 125", "200004": "Art. 144, I", "200005": "Art. 144, II",
    "200007": "Art. 145, I", "200008": "Art. 145, II", "200009": "Art. 146",
    "200010": "Art. 146, § 1º, I e II", "200011": "Art. 146, § 2º", "200030": "Art. 131",
    "200031": "Art. 132", "200032": "Art. 133", "200033": "Art. 133, § 1º", "200053": "Art. 146, § 1º, III",
}

# Alíquotas que a própria calculadora aplica no ano de teste (2026).
TESTE_2026 = {"cbs": "0.9", "ibsEstadual": "0.1", "ibsMunicipal": "0"}


def _q(v: Decimal) -> str:
    return str(v.quantize(D2, rounding=ROUND_HALF_UP))


def _erro(numero: int, code: str, title: str) -> dict:
    return {"nObj": numero, "estadoItem": "INCONSISTENCIA_ENTRADA",
            "erros": [{"code": code, "title": title, "detail": title}]}


def calcular(operacao: dict) -> dict:
    ano = int(operacao["dhFatoGerador"][:4])
    objetos = []
    for item in operacao["itens"]:
        if item["cClassTrib"] == "999999":
            objetos.append(_erro(item["numero"], "REG-011", "NCM não vinculada à Classificação Tributária"))
            continue
        aliq = item.get("aliquotasNominais")
        if ano < 2027 and aliq:
            objetos.append(_erro(item["numero"], "REG-036", "Alíquotas nominais informadas indevidamente"))
            continue
        if ano >= 2027 and not aliq:
            objetos.append(_erro(item["numero"], "REG-037", "Alíquotas nominais não informadas"))
            continue
        aliq = aliq or TESTE_2026
        trib = {}
        if "impostoSeletivo" in item:
            g = item["impostoSeletivo"]
            v_is = Decimal(str(g["baseCalculo"])) * Decimal(str(aliq["impostoSeletivo"]["adValorem"])) / 100
            trib["IS"] = {"vBCIS": _q(Decimal(str(g["baseCalculo"]))), "vIS": _q(v_is)}
            base = Decimal(str(g["baseCalculo"])) + Decimal(_q(v_is))
        else:
            c = item["gComponentesBC"]
            deducoes = ("vDesc", "vICMS", "vFCP") if ano < 2033 else ("vDesc",)  # ICMS extinto em 2033
            base = (sum(Decimal(str(c.get(k, 0))) for k in ("vProd", "vFrete", "vSeg", "vOutro"))
                    - sum(Decimal(str(c.get(k, 0))) for k in deducoes))
        cc = item["cClassTrib"]
        fator = Decimal("0") if cc in ZERO else Decimal("0.4") if cc in SESSENTA else Decimal("1")
        cbs = base * Decimal(str(aliq["cbs"])) / 100 * fator
        uf = base * Decimal(str(aliq["ibsEstadual"])) / 100 * fator
        mun = base * Decimal(str(aliq["ibsMunicipal"])) / 100 * fator
        objetos.append({
            "nObj": item["numero"], "estadoItem": "CALCULADO" if ano < 2027 else "CALCULO_SIMULADO",
            "tribCalc": {**trib, "IBSCBS": {"CST": item["cst"], "cClassTrib": item["cClassTrib"], "gIBSCBS": {
                "vBC": _q(base),
                "gIBSUF": {"vIBSUF": _q(uf)}, "gIBSMun": {"vIBSMun": _q(mun)},
                "vIBS": _q(Decimal(_q(uf)) + Decimal(_q(mun))),
                "gCBS": {"vCBS": _q(cbs)},
            }}},
        })
    return {"objetos": objetos}


class FakeCalculadora:
    def __init__(self, versao: str = "1.6.1") -> None:
        self.versao = versao
        self.recebidas: list[dict] = []
        self.consultas_ncm: list[tuple[str, str]] = []
        self.requisicoes: list[str] = []  # caminhos de todas as chamadas, na ordem
        self.falhas_restantes = 0         # próximas N chamadas respondem 503
        dono = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silencia o servidor nos testes
                pass

            def _json(self, status: int, corpo: object) -> None:
                dados = json.dumps(corpo).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(dados)))
                self.end_headers()
                self.wfile.write(dados)

            def _falhar(self) -> bool:
                with dono._lock:
                    dono.requisicoes.append(self.path)
                    if dono.falhas_restantes > 0:
                        dono.falhas_restantes -= 1
                        falhar = True
                    else:
                        falhar = False
                if falhar:
                    self._json(503, {"title": "Indisponível (dublê)"})
                return falhar

            def do_GET(self):
                from urllib.parse import parse_qs, urlparse
                if self._falhar():
                    return
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path.endswith("/ncm-aplicavel"):
                    dono.consultas_ncm.append((q["cClassTrib"], q["ncm"]))
                    valido = q["cClassTrib"] in MEDICAMENTOS or (q["cClassTrib"], q["ncm"]) in NCM_ACEITO
                    self._json(200, {"cClassTrib": q["cClassTrib"], "ncm": q["ncm"], "valido": valido})
                elif url.path.endswith("/dados-abertos/ncm"):
                    ncm = q.get("ncm", "")
                    if ncm in NCM_INEXISTENTES:
                        self._json(404, {"title": "NCM não encontrada"})
                    else:
                        self._json(200, {"ncm": ncm})
                elif url.path.endswith("/dados-abertos/versao"):
                    self._json(200, {"versaoApp": dono.versao, "versaoDb": "FAKE", "dataVersaoDb": "2026-01-01",
                                     "ambiente": "dublê"})
                elif "/cbs-ibs/class-trib/" in url.path:
                    cc = url.path.rsplit("/", 1)[1]
                    credita = cc not in SEM_CREDITO
                    self._json(200, {"codigo": cc, "indicaApropriacaoCreditoAdquirenteCbs": credita,
                                     "indicaApropriacaoCreditoAdquirenteIbs": credita})
                elif url.path.endswith("/fundamentacoes-legais"):
                    self._json(200, [{"codigoClassificacaoTributaria": cc, "textoCurto": t, "texto": t}
                                     for cc, t in FUNDAMENTACAO.items()])
                else:
                    self._json(200, [])

            def do_POST(self):
                corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self._falhar():
                    return
                if self.path.endswith("/observabilidade/regime-geral"):
                    dono.recebidas.append(corpo)
                    self._json(200, calcular(corpo))
                else:
                    self._json(404, {"erro": "não implementado no dublê"})

        self._lock = threading.Lock()
        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.servidor.server_port}/api"
        self._thread = threading.Thread(target=self.servidor.serve_forever, daemon=True)

    def __enter__(self) -> "FakeCalculadora":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.servidor.shutdown()
