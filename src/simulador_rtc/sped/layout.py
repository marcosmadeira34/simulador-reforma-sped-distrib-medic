"""Leiautes dos registros da EFD ICMS/IPI usados pelo simulador.

Cada lista traz os campos na ordem oficial do Guia Prático da EFD ICMS/IPI.
O índice 0 é sempre o nome do registro (REG).
"""

REG_0000 = [
    "REG", "COD_VER", "COD_FIN", "DT_INI", "DT_FIN", "NOME", "CNPJ", "CPF",
    "UF", "IE", "COD_MUN", "IM", "SUFRAMA", "IND_PERFIL", "IND_ATIV",
]

REG_0150 = [
    "REG", "COD_PART", "NOME", "COD_PAIS", "CNPJ", "CPF", "IE", "COD_MUN",
    "SUFRAMA", "END", "NUM", "COMPL", "BAIRRO",
]

REG_0200 = [
    "REG", "COD_ITEM", "DESCR_ITEM", "COD_BARRA", "COD_ANT_ITEM", "UNID_INV",
    "TIPO_ITEM", "COD_NCM", "EX_IPI", "COD_GEN", "COD_LST", "ALIQ_ICMS", "CEST",
]

REG_C100 = [
    "REG", "IND_OPER", "IND_EMIT", "COD_PART", "COD_MOD", "COD_SIT", "SER",
    "NUM_DOC", "CHV_NFE", "DT_DOC", "DT_E_S", "VL_DOC", "IND_PGTO", "VL_DESC",
    "VL_ABAT_NT", "VL_MERC", "IND_FRT", "VL_FRT", "VL_SEG", "VL_OUT_DA",
    "VL_BC_ICMS", "VL_ICMS", "VL_BC_ICMS_ST", "VL_ICMS_ST", "VL_IPI", "VL_PIS",
    "VL_COFINS", "VL_PIS_ST", "VL_COFINS_ST",
]

REG_C170 = [
    "REG", "NUM_ITEM", "COD_ITEM", "DESCR_COMPL", "QTD", "UNID", "VL_ITEM",
    "VL_DESC", "IND_MOV", "CST_ICMS", "CFOP", "COD_NAT", "VL_BC_ICMS",
    "ALIQ_ICMS", "VL_ICMS", "VL_BC_ICMS_ST", "ALIQ_ST", "VL_ICMS_ST",
    "IND_APUR", "CST_IPI", "COD_ENQ", "VL_BC_IPI", "ALIQ_IPI", "VL_IPI",
    "CST_PIS", "VL_BC_PIS", "ALIQ_PIS", "QUANT_BC_PIS", "ALIQ_PIS_QUANT",
    "VL_PIS", "CST_COFINS", "VL_BC_COFINS", "ALIQ_COFINS", "QUANT_BC_COFINS",
    "ALIQ_COFINS_QUANT", "VL_COFINS", "COD_CTA", "VL_ABAT_NT",
]

LEIAUTES = {
    "0000": REG_0000,
    "0150": REG_0150,
    "0200": REG_0200,
    "C100": REG_C100,
    "C170": REG_C170,
}

# Situações de documento consideradas na simulação (00 = regular, 01 = extemporâneo).
COD_SIT_VALIDOS = {"00", "01"}
