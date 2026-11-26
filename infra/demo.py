#!/usr/bin/env python3
"""Envia o exemplo da distribuidora ao S3 e inicia uma execução da Step Function.

    python infra/demo.py                      # transição 2026–2033
    python infra/demo.py --config config/saude.toml --esperar
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import boto3

from saidas import link_console, saidas

RAIZ = Path(__file__).resolve().parents[1]
PASTAS = ["config", "exemplos/saude"]
ENTRADAS = ["exemplos/saude/efd_icms_ipi_outubro.txt", "exemplos/saude/nfe"]


def enviar(bucket: str, prefixo: str) -> int:
    s3 = boto3.client("s3")
    n = 0
    for pasta in PASTAS:
        for arquivo in sorted((RAIZ / pasta).rglob("*")):
            if arquivo.is_file() and "Zone.Identifier" not in arquivo.name:
                s3.upload_file(str(arquivo), bucket, f"{prefixo}{arquivo.relative_to(RAIZ).as_posix()}")
                n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/saude_transicao.toml")
    ap.add_argument("--esperar", action="store_true", help="acompanha a execução até ela parar")
    args = ap.parse_args()

    out = saidas()
    nome = "demo-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    prefixo = f"entradas/{nome}/"
    print(f"Enviando {enviar(out['Bucket'], prefixo)} arquivos para s3://{out['Bucket']}/{prefixo}")

    entrada = {"prefixo": prefixo, "config": args.config, "entradas": ENTRADAS}
    sfn = boto3.client("stepfunctions")
    arn = sfn.start_execution(stateMachineArn=out["StateMachineArn"], name=nome,
                              input=json.dumps(entrada))["executionArn"]
    print(f"Execução iniciada: {nome}\nAcompanhe no console: {link_console(arn)}")

    if not args.esperar:
        return
    ultimo = None
    while True:
        status = sfn.describe_execution(executionArn=arn)["status"]
        estado = _estado_atual(sfn, arn)
        if (status, estado) != ultimo:
            print(f"  {status:<10} {estado or ''}")
            ultimo = (status, estado)
        if status != "RUNNING":
            break
        if estado == "AguardarAprovacao":
            print("  Esperando aprovação: python infra/aprovar.py aprovar --por \"SEU NOME\"")
            break
        time.sleep(10)


def _estado_atual(sfn, arn: str) -> str | None:
    eventos = sfn.get_execution_history(executionArn=arn, reverseOrder=True, maxResults=20)["events"]
    for e in eventos:
        if e["type"].endswith("StateEntered"):
            return e["stateEnteredEventDetails"]["name"]
    return None


if __name__ == "__main__":
    main()
