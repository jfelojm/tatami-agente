"""

Costo unitario canónico por MP para recetas y subrecetas.



Contrato Tatami (alineado con procesar_facturas_drive):

  - BD_ITEMS_PROV.precio_ref = USD por unidad_base (costo_efectivo ÷ factor), NO precio de bulto.

  - Solo dividir precio_ref/factor si la celda trae bulto (precio_ref > 0,05 y factor grande).



Lectura para recetas:

  1. Mediana robusta de precio_ref por MP (descarta basura ~0 y outliers).

  2. Si no hay prov usable: BD_MP_SISTEMA (corrigiendo pack en movimientos).

  3. Nunca elegir un costo ~0 si hay otro candidato plausible.

"""



from __future__ import annotations



from collections import defaultdict



from bodegas_config import normalizar_cod_bodega

from numeros_sheets import (

    canonical_costo_por_mp,

    expandir_costos_mp_unico,

    parse_numero_sheets,

    precio_ref_a_unidad_base,

)



# Por debajo de esto se considera basura (corrida errónea / celda vacía)

_PISO_COSTO_VALIDO = 1e-6

# Candado: rechazar escrituras automáticas con salto >10× o <0.1× sin catálogo
RATIO_MAX_CAMBIO_COSTO = 10.0
RATIO_MIN_CAMBIO_COSTO = 0.1

# Rango típico licores/vinos en ml (USD/ml) — no aplicar heurística gr/pack
_RANGO_CU_ML_LICOR = (0.03, 0.65)





def norm_mp(cod: str) -> str:

    s = (cod or "").strip()

    if not s:

        return ""

    if s.isdigit():

        return str(int(s))

    return s





def _mediana_robusta(valores: list[float]) -> float:

    """Mediana ignorando casi-ceros y outliers extremos."""

    sane = sorted(v for v in valores if v >= _PISO_COSTO_VALIDO)

    if not sane:

        return 0.0

    if len(sane) == 1:

        return sane[0]

    med = sane[len(sane) // 2]

    if med <= 0:

        return sane[0]

    filtrados = [v for v in sane if med / 100 <= v <= med * 100]

    if not filtrados:

        filtrados = sane

    filtrados.sort()

    n = len(filtrados)

    mid = n // 2

    if n % 2 == 1:

        return filtrados[mid]

    return round((filtrados[mid - 1] + filtrados[mid]) / 2.0, 6)





def cargar_factor_items_prov(sh) -> dict[str, float]:

    """cod_mp_norm -> factor_conversion típico (para corregir pack sin dividir)."""

    ws = sh.worksheet("BD_ITEMS_PROV")

    values = ws.get_all_values()

    hi = next(

        (i for i, r in enumerate(values) if any((c or "").strip() == "cod_mp_sistema" for c in r)),

        None,

    )

    if hi is None:

        return {}

    headers = [(c or "").strip() for c in values[hi]]

    ic = headers.index("cod_mp_sistema")

    ifac = headers.index("factor_conversion")

    out: dict[str, float] = {}

    for row in values[hi + 1 :]:

        cod = norm_mp(row[ic] if ic < len(row) else "")

        fac = parse_numero_sheets(row[ifac] if ifac < len(row) else 0, 0)

        if cod and fac > 1 and cod not in out:

            out[cod] = fac

    return out





def _corregir_cupo_pack(
    cu: float,
    factor_hint: float | None = None,
    *,
    unidad_base: str = "gr",
) -> tuple[float, bool]:
    """
    Si cu parece precio de kg/caja guardado como USD/gr, divide por factor.
    NO aplicar a licores ml en rango 0.03–0.25 USD/ml (bug histórico ÷1000).
    """
    u = (unidad_base or "gr").strip().lower()
    lo, hi = _RANGO_CU_ML_LICOR
    if u == "ml" and lo <= cu <= hi:
        return cu, False

    if cu <= 0 or not _parece_precio_pack_sin_dividir(cu, unidad_base):
        return cu, False

    candidatos = []
    if factor_hint and factor_hint > 1:
        candidatos.append(factor_hint)
    for fac in (1000.0, 700.0, 4000.0, 24.0):
        if fac not in candidatos:
            candidatos.append(fac)

    for fac in candidatos:
        c2 = round(cu / fac, 6)
        if 0 < c2 < 0.05:
            # Evitar sobre-corrección ~1000× en MPs ml mal clasificados como gr
            if u == "ml" and cu >= lo and c2 < cu / 50:
                continue
            return c2, True

    return cu, False


def validar_cambio_costo_ref(
    cu_old: float,
    cu_new: float,
    *,
    unidad_base: str = "",
    cu_prov: float = 0.0,
    forzar: bool = False,
) -> tuple[bool, str]:
    """
    Candado antes de escribir costo_unitario_ref en BD_MP_SISTEMA.
    Bloquea saltos >10× o <0.1× salvo que el catálogo confirme el nuevo valor.
    """
    if forzar or cu_old <= _PISO_COSTO_VALIDO:
        return True, "sin_anterior_o_forzado"
    if cu_new <= _PISO_COSTO_VALIDO:
        return False, "costo_nuevo_cero"
    ratio = cu_new / cu_old if cu_old > 0 else 0.0
    if RATIO_MIN_CAMBIO_COSTO <= ratio <= RATIO_MAX_CAMBIO_COSTO:
        return True, "ok"
    if cu_prov >= _PISO_COSTO_VALIDO:
        rel = abs(cu_new - cu_prov) / max(cu_prov, 1e-9)
        if rel <= 0.05:
            return True, "confirmado_catalogo"
    return False, f"salto_extremo_ratio_{ratio:.2f}"





def cargar_costo_desde_items_prov(sh) -> dict[str, float]:

    """cod_mp_norm -> USD/unidad_base (mediana robusta por MP, no min)."""

    ws = sh.worksheet("BD_ITEMS_PROV")

    values = ws.get_all_values()

    hi = next(

        (i for i, r in enumerate(values) if any((c or "").strip() == "cod_mp_sistema" for c in r)),

        None,

    )

    if hi is None:

        return {}

    headers = [(c or "").strip() for c in values[hi]]

    ic = headers.index("cod_mp_sistema")

    ip = headers.index("precio_ref")

    ifac = headers.index("factor_conversion")

    ia = headers.index("activo") if "activo" in headers else None



    por_mp: dict[str, list[float]] = defaultdict(list)

    for row in values[hi + 1 :]:

        if ia is not None and ia < len(row) and (row[ia] or "").strip().upper() == "NO":

            continue

        cod = norm_mp(row[ic] if ic < len(row) else "")

        if not cod:

            continue

        pr = parse_numero_sheets(row[ip] if ip < len(row) else 0)

        fac = parse_numero_sheets(row[ifac] if ifac < len(row) else 0, 0)

        if pr <= 0:

            continue

        cu = precio_ref_a_unidad_base(pr, fac or 1.0)

        if cu >= _PISO_COSTO_VALIDO:

            por_mp[cod].append(cu)

    return {k: round(_mediana_robusta(v), 6) for k, v in por_mp.items() if v}





def cargar_costo_desde_bd_mp(sh) -> dict[tuple[str, str], float]:

    """(mp_norm, bodega) -> costo en hoja BD_MP_SISTEMA."""

    values = sh.worksheet("BD_MP_SISTEMA").get_all_values()

    hi = next(i for i, r in enumerate(values) if "cod_mp_sistema" in (r or []))

    headers = [(c or "").strip() for c in values[hi]]

    ic = headers.index("cod_mp_sistema")

    ib = headers.index("cod_bodega")

    icu = headers.index("costo_unitario_ref")

    out: dict[tuple[str, str], float] = {}

    for row in values[hi + 1 :]:

        cod = norm_mp(row[ic] if ic < len(row) else "")

        bod = normalizar_cod_bodega(row[ib] if ib < len(row) else "")

        if not cod or not bod:

            continue

        cu = parse_numero_sheets(row[icu] if icu < len(row) else 0)

        key = (cod, bod)

        if key not in out or cu > 0:

            out[key] = cu

    return out





def _parece_precio_pack_sin_dividir(cu: float, unidad_base: str = "gr") -> bool:

    """Heurística: USD/gr > 0.05 suele ser precio de kg/caja mal cargado."""

    u = (unidad_base or "gr").strip().lower()

    if u == "uni":

        return cu > 50.0

    if u == "ml":

        return cu > 0.2

    return cu > 0.05





def _elegir_costo_mp_final(
    cu_prov: float,
    cu_hoja: float,
    *,
    umbral_pack: float = 5.0,
) -> tuple[float, str | None]:
    """
    Mediana robusta entre items_prov y BD_MP.
    Pack en precio_ref: precio_ref > 0,05 y BD_MP coherente y mucho menor.
    """
    lo, hi = _RANGO_CU_ML_LICOR
    # BD_MP corrupto ~÷1000 vs catálogo ml válido
    if (
        cu_prov >= lo
        and cu_prov <= hi
        and cu_hoja >= _PISO_COSTO_VALIDO
        and cu_hoja < cu_prov / 50
    ):
        return cu_prov, "bd_mp_div1000_usa_prov"

    if (
        cu_prov > 0.05
        and cu_hoja >= _PISO_COSTO_VALIDO
        and cu_hoja < cu_prov / umbral_pack
    ):
        # Licores ml: cu_prov ya es USD/ml; no descartar por regla pack/gr
        if lo <= cu_prov <= hi:
            return cu_prov, "prov_ml_valido_vs_hoja_baja"
        return cu_hoja, "prov_pack_usa_bd_mp"

    # Catálogo leído mal (re-dividió precio_ref unitario); BD_MP aún tiene costo de factura/manual.
    if (
        cu_hoja >= 0.02
        and cu_prov > 0
        and cu_prov < cu_hoja / umbral_pack
    ):
        return cu_hoja, "prov_lectura_baja_usa_bd_mp"

    candidatos = []
    if cu_prov >= _PISO_COSTO_VALIDO:
        candidatos.append(cu_prov)
    if cu_hoja >= _PISO_COSTO_VALIDO:
        candidatos.append(cu_hoja)
    if not candidatos:
        return 0.0, None
    cu = round(_mediana_robusta(candidatos), 6)
    nota = "mediana_prov_bd_mp" if len(candidatos) > 1 else None
    return cu, nota





def cargar_costos_mp_para_recetas(

    sh,

    *,

    umbral_pack: float = 5.0,

) -> tuple[dict[tuple[str, str], float], list[str]]:

    """

    Mapa (cod_mp_norm, cod_bodega) -> USD/unidad_base para líneas de receta/subreceta.

    """

    prov = cargar_costo_desde_items_prov(sh)

    factores = cargar_factor_items_prov(sh)

    hoja_mp = cargar_costo_desde_bd_mp(sh)

    avisos: list[str] = []

    # unidad_base por MP desde maestro (para no aplicar heurística gr a licores ml)
    unidad_mp: dict[str, str] = {}
    ws_mp = sh.worksheet("BD_MP_SISTEMA")
    vals_mp = ws_mp.get_all_values()
    hi_mp = next(i for i, r in enumerate(vals_mp) if "cod_mp_sistema" in r)
    h_mp = [(c or "").strip() for c in vals_mp[hi_mp]]
    ic_mp = h_mp.index("cod_mp_sistema")
    iu_mp = h_mp.index("unidad_base") if "unidad_base" in h_mp else None
    if iu_mp is not None:
        for row in vals_mp[hi_mp + 1 :]:
            cod = norm_mp(row[ic_mp] if ic_mp < len(row) else "")
            if cod and cod not in unidad_mp:
                unidad_mp[cod] = (row[iu_mp] if iu_mp < len(row) else "").strip().lower()



    bodegas = sorted({bod for _, bod in hoja_mp})

    if not bodegas:

        bodegas = ["BOD-001", "BOD-002"]



    out: dict[tuple[str, str], float] = {}

    mps = set(prov) | {mp for mp, _ in hoja_mp}



    for mp in mps:

        cu_prov = prov.get(mp, 0.0)

        cu_hoja_vals = [hoja_mp.get((mp, b), 0) for b in bodegas if hoja_mp.get((mp, b), 0) > 0]

        cu_hoja = min(cu_hoja_vals) if cu_hoja_vals else 0.0



        cu_final, nota = _elegir_costo_mp_final(cu_prov, cu_hoja, umbral_pack=umbral_pack)

        if cu_final <= 0:

            continue

        if nota:

            avisos.append(f"MP {mp}: {nota} -> {cu_final:.6f}")



        if cu_prov <= 0 and cu_hoja > 0:
            uni = unidad_mp.get(mp, "gr")
            lo, hi = _RANGO_CU_ML_LICOR
            if uni == "ml" and lo <= cu_hoja <= hi:
                cu_final = cu_hoja
            else:
                uni_hint = uni or ("ml" if (factores.get(mp) or 0) >= 300 else "gr")
                cu_corr, fixed = _corregir_cupo_pack(
                    cu_hoja, factores.get(mp), unidad_base=uni_hint
                )
                if fixed:
                    cu_final = cu_corr
                    avisos.append(
                        f"MP {mp}: BD_MP {cu_hoja:.4f} -> {cu_corr:.6f} USD/{uni_hint} (÷factor pack)"
                    )
                elif _parece_precio_pack_sin_dividir(cu_hoja, uni_hint):
                    avisos.append(
                        f"MP {mp}: BD_MP {cu_hoja:.4f} alto sin precio_ref (revisar manual)"
                    )



        for bod in bodegas:

            out[(mp, bod)] = cu_final



    return expandir_costos_mp_unico(out), avisos





def resolver_costo_ref_escritura(

    costo_mov: float | None,

    cu_prov: float = 0.0,

    factor_hint: float | None = None,

    *,

    umbral_pack: float = 5.0,

    unidad_base: str = "gr",

) -> float | None:

    """

    Costo a persistir en BD_MP_SISTEMA.costo_unitario_ref.



    - Con precio en catálogo prov: usa prov salvo que el promedio de ENTRADAs sea

      coherente (no supera prov × umbral_pack).

    - Sin prov: usa promedio mov corrigiendo pack (÷ factor) si aplica.

    """

    mov = float(costo_mov or 0)

    if cu_prov >= _PISO_COSTO_VALIDO:

        if mov >= _PISO_COSTO_VALIDO and mov <= cu_prov * umbral_pack:

            return round(mov, 6)

        return round(cu_prov, 6)

    if mov >= _PISO_COSTO_VALIDO:

        cu_corr, _ = _corregir_cupo_pack(mov, factor_hint, unidad_base=unidad_base)

        return round(cu_corr, 6)

    return None





def elegir_costo_mp(

    cod_mp: str,

    cod_bodega: str,

    costos: dict[tuple[str, str], float],

) -> tuple[float, str]:

    from numeros_sheets import elegir_costo_unitario_mp



    nk = norm_mp(cod_mp)

    if not nk:

        return 0.0, "sin_cod_mp"

    return elegir_costo_unitario_mp(costos, nk, cod_bodega)


