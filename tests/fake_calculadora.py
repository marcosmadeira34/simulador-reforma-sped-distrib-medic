"""Dublê de testes da Calculadora RTC.

NÃO calcula tributos de verdade: aplica uma regra simplificada só para testar o
fluxo do simulador (base = vProd - vDesc - vICMS; redução de 100% para o cClassTrib
200003). Para resultados reais, use a calculadora oficial (docker compose up).
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

NCM_INEXISTENTES = {"30022029", "40151100"}  # fora da tabela de NCM em 2027

# ncm-aplicavel: pares observados na calculadora 1.6.1 (seringa e cadeira de rodas)
# e pares assumidos só para o teste (dieta enteral).
NCM_ACEITO = {
    ("200030", "90183119"), ("200030", "30049099"), ("200007", "87139000"),
    ("200033", "21069090"), ("200011", "21069090"),
}


def _q(v: Decimal) -> str:
    return str(v.quantize(D2, rounding=ROUND_HALF_UP))


def calcular(operacao: dict) -> dict:
    objetos = []
    for item in operacao["itens"]:
        if item["cClassTrib"] == "999999":
            objetos.append({"nObj": item["numero"], "estadoItem": "INCONSISTENCIA_ENTRADA",
                            "erros": [{"code": "REG-011", "title": "NCM não vinculada",
                                       "detail": "NCM não vinculada à Classificação Tributária"}]})
            continue
        aliq = item["aliquotasNominais"]
        trib = {}
        if "impostoSeletivo" in item:
            g = item["impostoSeletivo"]
            v_is = Decimal(str(g["baseCalculo"])) * Decimal(str(aliq["impostoSeletivo"]["adValorem"])) / 100
            trib["IS"] = {"vBCIS": _q(Decimal(str(g["baseCalculo"]))), "vIS": _q(v_is)}
            base = Decimal(str(g["baseCalculo"])) + Decimal(_q(v_is))
        else:
            c = item["gComponentesBC"]
            base = (sum(Decimal(str(c.get(k, 0))) for k in ("vProd", "vFrete", "vSeg", "vOutro"))
                    - sum(Decimal(str(c.get(k, 0))) for k in ("vDesc", "vICMS", "vFCP")))
        cc = item["cClassTrib"]
        fator = Decimal("0") if cc in ZERO else Decimal("0.4") if cc in SESSENTA else Decimal("1")
        cbs = base * Decimal(str(aliq["cbs"])) / 100 * fator
        uf = base * Decimal(str(aliq["ibsEstadual"])) / 100 * fator
        mun = base * Decimal(str(aliq["ibsMunicipal"])) / 100 * fator
        objetos.append({
            "nObj": item["numero"], "estadoItem": "CALCULO_SIMULADO",
            "tribCalc": {**trib, "IBSCBS": {"CST": item["cst"], "cClassTrib": item["cClassTrib"], "gIBSCBS": {
                "vBC": _q(base),
                "gIBSUF": {"vIBSUF": _q(uf)}, "gIBSMun": {"vIBSMun": _q(mun)},
                "vIBS": _q(Decimal(_q(uf)) + Decimal(_q(mun))),
                "gCBS": {"vCBS": _q(cbs)},
            }}},
        })
    return {"objetos": objetos}


class FakeCalculadora:
    def __init__(self) -> None:
        self.recebidas: list[dict] = []
        self.consultas_ncm: list[tuple[str, str]] = []
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

            def do_GET(self):
                from urllib.parse import parse_qs, urlparse
                url = urlparse(self.path)
                if url.path.endswith("/ncm-aplicavel"):
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    dono.consultas_ncm.append((q["cClassTrib"], q["ncm"]))
                    valido = q["cClassTrib"] in MEDICAMENTOS or (q["cClassTrib"], q["ncm"]) in NCM_ACEITO
                    self._json(200, {"cClassTrib": q["cClassTrib"], "ncm": q["ncm"], "valido": valido})
                elif url.path.endswith("/dados-abertos/ncm"):
                    ncm = parse_qs(url.query).get("ncm", [""])[0]
                    if ncm in NCM_INEXISTENTES:
                        self._json(404, {"title": "NCM não encontrada"})
                    else:
                        self._json(200, {"ncm": ncm})
                else:
                    self._json(200, [])

            def do_POST(self):
                corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/observabilidade/regime-geral"):
                    dono.recebidas.append(corpo)
                    self._json(200, calcular(corpo))
                else:
                    self._json(404, {"erro": "não implementado no dublê"})

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.servidor.server_port}/api"
        self._thread = threading.Thread(target=self.servidor.serve_forever, daemon=True)

    def __enter__(self) -> "FakeCalculadora":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.servidor.shutdown()
