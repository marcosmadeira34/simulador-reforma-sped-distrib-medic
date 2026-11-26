"""Linha de comando: `simulador-rtc simular <entradas...>` e `simulador-rtc ping`."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import __version__
from .classificacao import Classificador
from .config import Config, carregar_cenarios
from .governanca import Governanca
from .modelos import Documento, Empresa
from .nfe.parser import NfeParser
from .relatorio import Totais, gravar
from .rtc.client import CalculadoraClient
from .setores.saude import ClassificadorSaude
from .simulacao import Simulador
from .sped.parser import EfdIcmsIpiParser

log = logging.getLogger(__name__)


class VersaoDivergente(RuntimeError):
    pass


def _parser(caminho: str):
    """Pasta ou .xml = NF-e; qualquer outro arquivo = EFD ICMS/IPI."""
    p = Path(caminho)
    if p.is_dir() or p.suffix.lower() == ".xml":
        return NfeParser(p)
    return EfdIcmsIpiParser(p)


def _empresa_de(parser) -> Empresa | None:
    return parser.empresa if isinstance(parser, NfeParser) else parser.info.empresa


def _digitos(valor: str) -> str:
    return "".join(c for c in valor if c.isdigit())


class Fontes:
    """Uma ou mais entradas da mesma empresa, lidas em sequência.

    Típico: a EFD ICMS/IPI (entradas, com C170) + os XMLs das NF-e de saída.
    """

    def __init__(self, caminhos: list[str]) -> None:
        self.parsers = [_parser(c) for c in caminhos]
        self.lidos: set[str] = set()
        self._atual = None
        self._empresa: Empresa | None = None

    def documentos(self):
        for parser in self.parsers:
            self._atual = parser
            for doc in parser.documentos():
                self.lidos.add(doc.identificador)
                yield doc

    def empresa(self) -> Empresa:
        e = _empresa_de(self._atual) if self._atual else None
        if e is None:
            raise ValueError("Empresa não identificada (registro 0000 ou emitente da NF-e).")
        if self._empresa is None:
            self._empresa = e
        elif _digitos(e.cnpj) != _digitos(self._empresa.cnpj):
            raise ValueError(f"Entradas de empresas diferentes: {self._empresa.cnpj} e {e.cnpj}.")
        return self._empresa

    def ignorados(self) -> int:
        return sum(len(p.ignorados) if isinstance(p, NfeParser) else p.info.documentos_ignorados
                   for p in self.parsers)

    def sem_itens_nao_cobertos(self) -> list[str]:
        """Notas da EFD sem C170 cujo XML não estava entre as entradas: ficaram fora da simulação."""
        return [d for p in self.parsers if isinstance(p, EfdIcmsIpiParser)
                for d in p.info.documentos_sem_itens if d not in self.lidos]


def _classificador(config: Config, cliente: CalculadoraClient):
    if config.setor == "saude":
        if not (config.cadastro_produtos and config.cadastro_participantes):
            raise ValueError("Setor saude exige [saude] cadastro_produtos e cadastro_participantes.")
        return ClassificadorSaude.de_csv(cliente, config.data_fato_gerador,
                                         config.cadastro_produtos, config.cadastro_participantes)
    return Classificador.de_csv(config.classificacao_csv)


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _impressao(caminho: str | Path) -> dict:
    """Identifica um arquivo (ou pasta de XMLs) pelo conteúdo, para reproduzir a execução."""
    p = Path(caminho)
    if p.is_dir():
        arquivos = sorted(p.glob("*.xml"))
        h = hashlib.sha256()
        for a in arquivos:
            h.update(f"{a.name}:{_sha256(a)}\n".encode())
        return {"caminho": str(p), "arquivos": len(arquivos), "sha256": h.hexdigest()}
    return {"caminho": str(p), "sha256": _sha256(p)}


def _arquivos_de_regra(caminho_config: str, config: Config) -> list[dict]:
    regras = [Path(caminho_config)]
    if config.setor == "saude":
        regras += [config.cadastro_produtos, config.cadastro_participantes]
    else:
        regras.append(config.classificacao_csv)
    if config.aprovacoes_csv:
        regras.append(config.aprovacoes_csv)
    return [_impressao(r) for r in regras if r]


def _conferir_versao(cliente: CalculadoraClient, config: Config) -> dict:
    versao = cliente.versao()
    esperada = config.versao_calculadora
    if esperada and versao.get("versaoApp") != esperada:
        raise VersaoDivergente(
            f"A calculadora está na versão {versao.get('versaoApp')} (base {versao.get('versaoDb')}), "
            f"mas a configuração espera {esperada}. Rode os testes de contrato contra a nova versão "
            "(RTC_URL=... pytest -m contrato) e só então atualize [calculadora] versao_esperada.")
    return versao


def _cenario_json(config: Config) -> dict:
    a = config.aliquotas
    return {
        "nome": config.cenario, "setor": config.setor, "dh_fato_gerador": config.dh_fato_gerador.isoformat(),
        "aliquotas_nominais": None if a is None else
        {"cbs": str(a.cbs), "ibs_estadual": str(a.ibs_estadual), "ibs_municipal": str(a.ibs_municipal)},
        "tributos_remanescentes": {t: str(f) for t, f in sorted(config.tributos_remanescentes.items())},
        "creditos_atuais": sorted(config.creditos_atuais),
        "cbs_ibs_na_carga": config.cbs_ibs_na_carga,
        "validar_ncm": config.validar_ncm, "exigir_aprovacao": config.exigir_aprovacao,
    }


def _totais_json(t: Totais, sem_itens: list[str]) -> dict:
    s = lambda v: str(v.quantize(Decimal("0.01")))  # noqa: E731
    return {
        "itens": t.itens, "calculados": t.calculados, "pendentes": t.pendentes, "em_revisao": t.em_revisao,
        "por_estado": dict(sorted(t.por_estado.items())),
        "debito_atual": s(t.debito_atual), "debito_novo": s(t.debito_novo),
        "credito_atual": s(t.credito_atual), "credito_novo": s(t.credito_novo),
        "saldo_atual": s(t.saldo_atual), "saldo_novo": s(t.saldo_novo),
        "diferenca_liquida": s(t.diferenca_liquida),
        "notas_sem_itens_nao_cobertas": len(sem_itens),
    }


def _rodar_cenario(config: Config, args, cliente: CalculadoraClient, pasta: Path,
                   registro: dict) -> Totais:
    fontes = Fontes(args.entrada)
    governanca = Governanca.da_config(cliente, config)
    simulador = Simulador(config, _classificador(config, cliente), cliente, governanca)
    inicio = time.perf_counter()
    totais = gravar(simulador.simular(fontes.documentos(), fontes.empresa), pasta)
    seg = time.perf_counter() - inicio
    sem_itens = fontes.sem_itens_nao_cobertos()

    if sem_itens:
        with (pasta / "notas_sem_itens.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["documento"])
            w.writerows([d] for d in sem_itens)
    (pasta / "execucao.json").write_text(json.dumps(
        {**registro, "cenario": _cenario_json(config), "duracao_s": round(seg, 2),
         "totais": _totais_json(totais, sem_itens)}, ensure_ascii=False, indent=2), encoding="utf-8")

    emp = fontes._empresa
    titulo = f" | cenário: {config.cenario}" if config.cenario else ""
    print(f"\nEmpresa: {emp.nome if emp else '?'} ({emp.cnpj if emp else '?'}) | setor: {config.setor}{titulo}")
    print(f"Data simulada: {config.data_fato_gerador}")
    print(f"Itens: {totais.itens} | calculados: {totais.calculados} | pendentes: {totais.pendentes} "
          f"| para revisão: {totais.em_revisao} | documentos ignorados: {fontes.ignorados()}")
    for estado, n in sorted(totais.por_estado.items()):
        print(f"  {estado}: {n}")
    if sem_itens:
        print(f"ATENÇÃO: {len(sem_itens)} nota(s) da EFD sem C170 e sem o XML correspondente ficaram fora "
              f"(veja notas_sem_itens.csv). Inclua os XMLs dessas notas.")
    rem = "+".join(f"{t}" if f == 1 else f"{t}×{f}" for t, f in sorted(config.tributos_remanescentes.items()))
    print(f"Débitos (saídas)   atual: R$ {totais.debito_atual:>14,.2f} | cenário: R$ {totais.debito_novo:>14,.2f}")
    print(f"Créditos (entradas) atual: R$ {totais.credito_atual:>13,.2f} | cenário: R$ {totais.credito_novo:>14,.2f}")
    print(f"Saldo a pagar      atual: R$ {totais.saldo_atual:>14,.2f} | cenário: R$ {totais.saldo_novo:>14,.2f}")
    print(f"Diferença líquida: R$ {totais.diferenca_liquida:,.2f}  "
          f"(cenário = CBS+IBS+IS{'+' + rem if rem else ''}"
          f"{'; CBS/IBS de teste fora da carga' if not config.cbs_ibs_na_carga else ''})")
    print(f"Tempo: {seg:.1f}s | resultados em {pasta}/")
    return totais


def _simular(args: argparse.Namespace) -> int:
    cenarios = carregar_cenarios(args.config)
    base = cenarios[0]
    cliente = CalculadoraClient(base.url_calculadora, timeout_s=base.timeout_s)
    if not cliente.disponivel():
        print(f"Calculadora não responde em {base.url_calculadora}. Suba com `docker compose up -d`.",
              file=sys.stderr)
        return 2
    try:
        versao = _conferir_versao(cliente, base)
    except VersaoDivergente as exc:
        print(str(exc), file=sys.stderr)
        return 3

    registro = {
        "simulador": __version__,
        "executado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "calculadora": {"url": base.url_calculadora, **versao},
        "entradas": [_impressao(e) for e in args.entrada],
        "regras": _arquivos_de_regra(args.config, base),
    }
    saida = Path(args.saida)
    resumo = []
    for config in cenarios:
        pasta = saida / config.cenario if len(cenarios) > 1 else saida
        resumo.append((config, _rodar_cenario(config, args, cliente, pasta, registro)))

    if len(cenarios) > 1:
        with (saida / "cenarios.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["cenario", "data", "itens", "calculados", "pendentes", "debito_atual", "debito_novo",
                        "credito_atual", "credito_novo", "saldo_atual", "saldo_novo", "diferenca_liquida"])
            for c, t in resumo:
                w.writerow([c.cenario, c.data_fato_gerador, t.itens, t.calculados, t.pendentes,
                            *(f"{v:.2f}" for v in (t.debito_atual, t.debito_novo, t.credito_atual,
                                                   t.credito_novo, t.saldo_atual, t.saldo_novo,
                                                   t.diferenca_liquida))])
        print(f"\nResumo dos {len(cenarios)} cenários em {saida / 'cenarios.csv'}")
    return 0


def _ping(args: argparse.Namespace) -> int:
    config = carregar_cenarios(args.config)[0]
    cliente = CalculadoraClient(config.url_calculadora)
    if not cliente.disponivel():
        print("Calculadora indisponível")
        return 2
    v = cliente.versao()
    print(f"Calculadora disponível: versão {v.get('versaoApp')}, base {v.get('versaoDb')} "
          f"({v.get('dataVersaoDb')})")
    if config.versao_calculadora and v.get("versaoApp") != config.versao_calculadora:
        print(f"ATENÇÃO: a configuração espera a versão {config.versao_calculadora}")
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="simulador-rtc",
                                 description="Simulador da Reforma Tributária (CBS/IBS/IS) sobre o SPED")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simular", help="Simula a Reforma sobre EFD ICMS/IPI e/ou XMLs de NF-e")
    s.add_argument("entrada", nargs="+",
                   help="Arquivos .txt da EFD ICMS/IPI, XMLs de NF-e ou pastas de XMLs (da mesma empresa)")
    s.add_argument("--config", default="config/simulacao.toml")
    s.add_argument("--saida", default="resultados")
    s.set_defaults(func=_simular)

    p = sub.add_parser("ping", help="Verifica se a calculadora está no ar e qual a versão")
    p.add_argument("--config", default="config/simulacao.toml")
    p.set_defaults(func=_ping)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)
