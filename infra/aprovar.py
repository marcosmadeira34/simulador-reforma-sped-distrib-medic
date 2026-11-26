#!/usr/bin/env python3
"""Devolve o task token à Step Function que está parada em AguardarAprovacao.

    python infra/aprovar.py aprovar --por "Maria (tributário)"
    python infra/aprovar.py reprovar --motivo "Regra saude:med-anvisa ainda em discussão"

Sem --execucao, usa a execução mais recente em andamento. O token é lido do histórico da
execução (atributo task_token da mensagem SNS); também pode ser passado com --token.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import boto3

from saidas import link_console, saidas


def execucao_em_andamento(sfn) -> str:
    execucoes = sfn.list_executions(stateMachineArn=saidas()["StateMachineArn"], statusFilter="RUNNING",
                                    maxResults=1)["executions"]
    if not execucoes:
        raise SystemExit("Nenhuma execução em andamento.")
    return execucoes[0]["executionArn"]


def token_da_execucao(sfn, arn: str) -> str:
    paginas = sfn.get_paginator("get_execution_history").paginate(executionArn=arn, reverseOrder=True)
    for pagina in paginas:
        for evento in pagina["events"]:
            d = evento.get("taskScheduledEventDetails")
            if d and d["resourceType"] == "sns" and d["resource"] == "publish.waitForTaskToken":
                return json.loads(d["parameters"])["MessageAttributes"]["task_token"]["StringValue"]
    raise SystemExit("A execução não chegou ao estado AguardarAprovacao.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("acao", choices=["aprovar", "reprovar"])
    ap.add_argument("--execucao", help="ARN da execução (padrão: a mais recente em andamento)")
    ap.add_argument("--token", help="task token, se já o tiver (dispensa --execucao)")
    ap.add_argument("--por", default="", help="quem aprovou")
    ap.add_argument("--motivo", default="", help="motivo da reprovação")
    args = ap.parse_args()

    sfn = boto3.client("stepfunctions")
    arn = None
    if args.token:
        token = args.token
    else:
        arn = args.execucao or execucao_em_andamento(sfn)
        token = token_da_execucao(sfn, arn)

    if args.acao == "aprovar":
        if not args.por:
            raise SystemExit("Informe --por (quem aprovou): fica registrado no histórico da execução.")
        sfn.send_task_success(taskToken=token, output=json.dumps({
            "aprovado_por": args.por,
            "aprovado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False))
        print("Aprovado: a execução segue para Concluido.")
    else:
        sfn.send_task_failure(taskToken=token, error="Reprovado", cause=args.motivo or "Reprovado")
        print("Reprovado: a execução termina no estado Reprovado.")
    if arn:
        print(link_console(arn))


if __name__ == "__main__":
    main()
