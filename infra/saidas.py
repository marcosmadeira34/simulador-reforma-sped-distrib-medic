"""Lê as saídas (Outputs) do stack publicado: bucket, ARN da máquina de estados etc."""

from __future__ import annotations

import boto3

STACK = "SimuladorRtc"


def saidas(stack: str = STACK) -> dict[str, str]:
    resposta = boto3.client("cloudformation").describe_stacks(StackName=stack)
    return {o["OutputKey"]: o["OutputValue"] for o in resposta["Stacks"][0].get("Outputs", [])}


def link_console(arn_execucao: str) -> str:
    regiao = arn_execucao.split(":")[3]
    return (f"https://{regiao}.console.aws.amazon.com/states/home?region={regiao}"
            f"#/v2/executions/details/{arn_execucao}")
