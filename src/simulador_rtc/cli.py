"""Linha de comando: `simulador-rtc simular <efd.txt>` e `simulador-rtc ping`."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .classificacao import Classificador
from .config import Config
from .relatorio import gravar
from .rtc.client import CalculadoraClient
from .simulacao import Simulador
from .nfe.parser import NfeParser
from .setores.saude import ClassificadorSaude
from .sped.parser import EfdIcmsIpiParser


def _fonte(caminho: str):
    """Pasta ou .xml = NF-e; qualquer outro arquivo = EFD ICMS/IPI."""
    from pathlib import Path
    p = Path(caminho)
    if p.is_dir() or p.suffix.lower() == ".xml":
        parser = NfeParser(p)
        return parser, lambda: parser.empresa, lambda: len(parser.ignorados)
    parser = EfdIcmsIpiParser(p)
    return parser, lambda: parser.info.empresa, lambda: parser.info.documentos_ignorados


def _classificador(config: Config, cliente: CalculadoraClient):
    if config.setor == "saude":
        if not (config.cadastro_produtos and config.cadastro_participantes):
            raise ValueError("Setor saude exige [saude] cadastro_produtos e cadastro_participantes.")
        return ClassificadorSaude.de_csv(cliente, config.dh_fato_gerador.date().isoformat(),
                                         config.cadastro_produtos, config.cadastro_participantes)
    return Classificador.de_csv(config.classificacao_csv)


def _simular(args: argparse.Namespace) -> int:
    config = Config.de_toml(args.config)
    cliente = CalculadoraClient(config.url_calculadora, timeout_s=config.timeout_s)
    if not cliente.disponivel():
        print(f"Calculadora não responde em {config.url_calculadora}. Suba com `docker compose up -d`.",
              file=sys.stderr)
        return 2

    parser, empresa_de, ignorados = _fonte(args.entrada)
    simulador = Simulador(config, _classificador(config, cliente), cliente)
    inicio = time.perf_counter()

    def empresa():
        if empresa_de() is None:
            raise ValueError("Empresa não identificada (registro 0000 ou emitente da NF-e).")
        return empresa_de()

    totais = gravar(simulador.simular(parser.documentos(), empresa), args.saida)
    seg = time.perf_counter() - inicio

    emp = empresa_de()
    print(f"\nEmpresa: {emp.nome if emp else '?'} ({emp.cnpj if emp else '?'}) | setor: {config.setor}")
    print(f"Itens: {totais.itens} | calculados: {totais.calculados} | pendentes: {totais.pendentes} "
          f"| para revisão: {totais.em_revisao} | documentos ignorados: {ignorados()}")
    for estado, n in sorted(totais.por_estado.items()):
        print(f"  {estado}: {n}")
    print(f"Carga atual (ICMS+ST+IPI+PIS+COFINS): R$ {totais.carga_atual:,.2f}")
    rem = "+".join(sorted(config.tributos_remanescentes))
    print(f"Carga simulada (CBS+IBS+IS{'+' + rem if rem else ''}): R$ {totais.carga_nova:,.2f}")
    print(f"Diferença:                            R$ {totais.diferenca:,.2f}")
    print(f"Tempo: {seg:.1f}s | resultados em {args.saida}/")
    return 0


def _ping(args: argparse.Namespace) -> int:
    config = Config.de_toml(args.config)
    ok = CalculadoraClient(config.url_calculadora).disponivel()
    print("Calculadora disponível" if ok else "Calculadora indisponível")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="simulador-rtc",
                                 description="Simula CBS/IBS/IS sobre itens de uma EFD ICMS/IPI ou de NF-e.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="comando", required=True)

    s = sub.add_parser("simular", help="Simula a Reforma sobre uma EFD ICMS/IPI ou XMLs de NF-e")
    s.add_argument("entrada", help="Arquivo .txt da EFD ICMS/IPI, um XML de NF-e ou uma pasta de XMLs")
    s.add_argument("--config", default="config/simulacao.toml")
    s.add_argument("--saida", default="resultados")
    s.set_defaults(func=_simular)

    p = sub.add_parser("ping", help="Verifica se a calculadora está no ar")
    p.add_argument("--config", default="config/simulacao.toml")
    p.set_defaults(func=_ping)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
