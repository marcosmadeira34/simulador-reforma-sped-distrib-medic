"""Handlers Lambda: cada estado Task da Step Function chama uma função deste módulo.

A regra de negócio é a mesma da CLI (cli.py). Aqui só se traduz "evento JSON + arquivos no S3"
para "argumentos + arquivos locais" e de volta. Quem decide ordem, paralelismo, retentativas e
aprovação é a máquina de estados (infra/state_machine.asl.json), não este código.

Evento de entrada da execução (o que você passa ao iniciar a Step Function):
    {"prefixo": "entradas/demo/", "config": "config/saude_transicao.toml",
     "entradas": ["exemplos/saude/efd_icms_ipi_outubro.txt", "exemplos/saude/nfe"]}

`config` e `entradas` são relativos ao `prefixo` no bucket.

Erros que a Step Function trata pelo nome (Retry/Catch), porque o nome da classe da exceção vira
o `Error` do estado:
    CalculadoraIndisponivel  -> Retry (o container pode estar subindo)
    VersaoDivergente         -> Catch -> estado Fail
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Protocol

from .. import __version__
from ..cli import _arquivos_de_regra, _conferir_versao, _impressao, _rodar_cenario
from ..config import Config, carregar_cenarios
from ..rtc.client import CalculadoraClient, CalculadoraIndisponivel

log = logging.getLogger(__name__)
logging.getLogger().setLevel(logging.INFO)


# --- armazenamento --------------------------------------------------------------------------

class Armazenamento(Protocol):
    def baixar(self, prefixo: str, destino: Path) -> None: ...
    def enviar(self, origem: Path, prefixo: str) -> None: ...
    def gravar(self, chave: str, conteudo: str) -> None: ...
    def uri(self, chave: str) -> str: ...


class S3:
    def __init__(self, bucket: str, cliente=None) -> None:
        import boto3  # já vem no runtime Python da Lambda

        self.bucket = bucket
        self.s3 = cliente or boto3.client("s3")

    def baixar(self, prefixo: str, destino: Path) -> None:
        prefixo = prefixo.rstrip("/") + "/"
        for pagina in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefixo):
            for obj in pagina.get("Contents", []):
                relativo = obj["Key"][len(prefixo):]
                if not relativo or relativo.endswith("/"):
                    continue
                alvo = destino / relativo
                alvo.parent.mkdir(parents=True, exist_ok=True)
                self.s3.download_file(self.bucket, obj["Key"], str(alvo))

    def enviar(self, origem: Path, prefixo: str) -> None:
        for arquivo in sorted(origem.rglob("*")):
            if arquivo.is_file():
                chave = f"{prefixo.rstrip('/')}/{arquivo.relative_to(origem).as_posix()}"
                self.s3.upload_file(str(arquivo), self.bucket, chave)

    def gravar(self, chave: str, conteudo: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=chave, Body=conteudo.encode("utf-8"))

    def uri(self, chave: str) -> str:
        return f"s3://{self.bucket}/{chave}"


class Local:
    """Pasta local no lugar do S3: para os testes e para estudar os handlers sem conta AWS."""

    def __init__(self, raiz: str | Path) -> None:
        self.raiz = Path(raiz)

    def baixar(self, prefixo: str, destino: Path) -> None:
        shutil.copytree(self.raiz / prefixo, destino, dirs_exist_ok=True)

    def enviar(self, origem: Path, prefixo: str) -> None:
        shutil.copytree(origem, self.raiz / prefixo, dirs_exist_ok=True)

    def gravar(self, chave: str, conteudo: str) -> None:
        alvo = self.raiz / chave
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text(conteudo, encoding="utf-8")

    def uri(self, chave: str) -> str:
        return str(self.raiz / chave)


def _armazenamento() -> Armazenamento:
    local = os.environ.get("ARMAZENAMENTO_LOCAL")
    return Local(local) if local else S3(os.environ["BUCKET"])


@contextmanager
def _area_de_trabalho(arm: Armazenamento, prefixo: str) -> Iterator[Path]:
    """Baixa as entradas para um diretório temporário (o /tmp da Lambda é reaproveitado entre
    invocações, então sempre limpa no final)."""
    raiz = Path(tempfile.mkdtemp(prefix="rtc-"))
    try:
        arm.baixar(prefixo, raiz)
        yield raiz
    finally:
        shutil.rmtree(raiz, ignore_errors=True)


def _cenarios(raiz: Path, evento: dict) -> list[Config]:
    """Carrega a config; na AWS a URL da calculadora vem do ambiente (Cloud Map), não do TOML."""
    cenarios = carregar_cenarios(raiz / evento["config"])
    url = os.environ.get("RTC_URL")
    return [replace(c, url_calculadora=url.rstrip("/")) for c in cenarios] if url else cenarios


def _relativo(impressao: dict, raiz: Path) -> dict:
    """Troca o caminho temporário (/tmp/rtc-xyz/...) pelo caminho relativo ao prefixo."""
    caminho = Path(impressao["caminho"])
    try:
        caminho = caminho.relative_to(raiz)
    except ValueError:
        pass
    return {**impressao, "caminho": caminho.as_posix()}


# --- estados --------------------------------------------------------------------------------

def conferir(evento: dict, contexto=None) -> dict:
    """Estado ConferirCalculadora: calculadora no ar e na versão esperada; lista os cenários."""
    arm = _armazenamento()
    with _area_de_trabalho(arm, evento["prefixo"]) as raiz:
        cenarios = _cenarios(raiz, evento)
        base = cenarios[0]
        cliente = CalculadoraClient(base.url_calculadora, timeout_s=base.timeout_s)
        if not cliente.disponivel():
            raise CalculadoraIndisponivel(f"Calculadora não responde em {base.url_calculadora}")
        versao = _conferir_versao(cliente, base)  # VersaoDivergente sobe para a Step Function

        registro = {
            "simulador": __version__,
            "executado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "calculadora": {"url": base.url_calculadora, **versao},
            "entradas": [_relativo(_impressao(raiz / e), raiz) for e in evento["entradas"]],
            "regras": [_relativo(r, raiz) for r in _arquivos_de_regra(str(raiz / evento["config"]), base)],
        }
        return {
            "registro": registro,
            "cenarios": [{"indice": i, "nome": c.cenario or "unico"} for i, c in enumerate(cenarios)],
        }


def simular_cenario(evento: dict, contexto=None) -> dict:
    """Estado SimularCenario (uma iteração do Map): roda um cenário e grava os CSVs no S3.

    Devolve só os totais: o payload entre estados é limitado a 256 KB, então os arquivos
    ficam no S3 e trafega apenas o endereço deles.
    """
    arm = _armazenamento()
    nome = evento["cenario"]["nome"]
    with _area_de_trabalho(arm, evento["prefixo"]) as raiz:
        config = _cenarios(raiz, evento)[evento["cenario"]["indice"]]
        cliente = CalculadoraClient(config.url_calculadora, timeout_s=config.timeout_s)
        pasta = raiz / "_saida"
        args = SimpleNamespace(entrada=[str(raiz / e) for e in evento["entradas"]])
        _rodar_cenario(config, args, cliente, pasta, evento["registro"])

        execucao = json.loads((pasta / "execucao.json").read_text(encoding="utf-8"))
        destino = f"resultados/{evento['execucao']}/{nome}"
        arm.enviar(pasta, destino)
        return {"cenario": nome, "data": config.data_fato_gerador, "totais": execucao["totais"],
                "resultados": arm.uri(destino)}


COLUNAS_CENARIOS = ["cenario", "data", "itens", "calculados", "pendentes", "debito_atual", "debito_novo",
                    "credito_atual", "credito_novo", "saldo_atual", "saldo_novo", "diferenca_liquida"]


def consolidar(evento: dict, contexto=None) -> dict:
    """Estado Consolidar: junta os cenários em cenarios.csv (o mesmo resumo da CLI)."""
    arm = _armazenamento()
    resultados = evento["resultados"]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(COLUNAS_CENARIOS)
    for r in resultados:
        t = r["totais"]
        w.writerow([r["cenario"], r["data"], *(t[c] for c in COLUNAS_CENARIOS[2:])])
    chave = f"resultados/{evento['execucao']}/cenarios.csv"
    arm.gravar(chave, buf.getvalue())
    return {
        "cenarios_csv": arm.uri(chave),
        "em_revisao": sum(r["totais"]["em_revisao"] for r in resultados),
        "resumo": [{"cenario": r["cenario"], "diferenca_liquida": r["totais"]["diferenca_liquida"],
                    "em_revisao": r["totais"]["em_revisao"]} for r in resultados],
    }
